"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from app.entrypoints.review_interactive import run_interactive_review
from app.entrypoints.run import (
    _LOCAL_EXEC_STAGE_CHOICES,
    _build_assembly_selection,
    _interactive_gate_loop,
    _load_dotenv_without_override,
    _parse_args,
    _resolve_checkpointer_backend,
    _resolve_data_dir,
    _run_inspect,
    _run_resume,
    _run_session,
    _run_storage_audit,
    generate_session_id,
)
from app.entrypoints.tui import _tui_main, run_tui
from app.execution.env_resolver import PreflightResult
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    PersistedReviewRequest,
    load_review_request,
    persist_review_request,
)
from app.schemas.artifacts import ArtifactRef
from app.schemas.run_config import AssemblyRunConfig, LocalExecStage
from app.storage.artifacts import read_artifact_records
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout, locate_session_data_dir
from tests.data_v2 import v2_run_dir


class _FakeGraph:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.invocations: list[tuple[dict, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return self.result


class _FakeResumeGraph:
    def __init__(self, state: dict, result: dict | None = None) -> None:
        self.state = state
        self.result = result or {
            "current_stage": "done",
            "supervision_state": "completed",
            "route_trace": [],
        }
        self.updated: list[dict] = []
        self.invocations: list[tuple[dict | None, dict]] = []

    def get_state(self, config):
        return SimpleNamespace(values=self.state)

    def update_state(self, config, update):
        self.updated.append(update)

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return self.result


def _artifact_ref_json(
    candidate_id: str,
    *,
    artifact_type: str = "assembly.draft_assembly",
    suffix: str = ".fasta",
) -> dict:
    return ArtifactRef(
        artifact_id=f"artifact-{candidate_id}{suffix}",
        path_ref=f"artifacts/assembly/{candidate_id}{suffix}",
        sha256="a" * 64,
        size_bytes=10,
        artifact_type=artifact_type,
    ).model_dump(mode="json")


def _write_assembly_review_request(
    data_root: Path,
    session_id: str,
) -> tuple[Path, PersistedReviewRequest]:
    run_dir = v2_run_dir(data_root, session_id)
    paths = SessionPaths.from_run_dir(run_dir)
    payload = ReviewPayload(
        stage="assembly_review",
        gate_id="assembly_review",
        reason="needs_review",
        candidates=[
            {
                "candidate_id": "flye",
                "primary_output_ref": _artifact_ref_json("flye"),
            }
        ],
        decisions_allowed=["accept", "stop_human_review"],
        auto_decidable=False,
        report_refs={
            "assembly_review_report": "reports/assembly_review.md",
            "review_packet": "artifacts/assembly_review/assembly_review_packet.json",
        },
        candidate_comparison_context={
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {
                "flye": _artifact_ref_json("flye")
            },
        },
    )
    state = {
        "session_id": session_id,
        "data_dir": str(run_dir),
        "input_manifest_ref": paths.to_ref(paths.manifest_path),
    }
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason="needs_review",
        payload=payload,
        method_name="assembly_review.contract",
        artifact_type="assembly_review.request",
    )
    return run_dir, persisted


def _artifact_types(data_dir: Path, session_id: str) -> list[str]:
    session_dir = locate_session_data_dir(data_dir, session_id)
    return [
        record["artifact_type"]
        for record in read_artifact_records(session_dir, session_id)
    ]


def _load_assembly_review_request(
    data_root: Path,
    session_id: str,
) -> tuple[Path, ArtifactRef, dict]:
    run_dir = locate_session_data_dir(data_root, session_id)
    if run_dir is None:
        raise AssertionError(f"missing v2 run for {session_id}")
    record = next(
        item
        for item in read_artifact_records(run_dir, session_id)
        if item["artifact_type"] == "assembly_review.request"
    )
    ref = ArtifactRef.model_validate(
        {
            key: record[key]
            for key in (
                "schema_version",
                "artifact_id",
                "path_ref",
                "sha256",
                "size_bytes",
                "artifact_type",
            )
        }
    )
    return run_dir, ref, load_review_request(run_dir, ref, session_id=session_id)


def _add_gene_method_bins(bin_dir: Path) -> Path:
    """Add fake augustus + gffread binaries so a gene candidate can materialize."""
    augustus = bin_dir / "augustus"
    augustus.write_text(
        "#!/bin/sh\n"
        "printf '##gff-version 3\\n"
        "ctg1\\tAUGUSTUS\\tgene\\t1\\t6\\t.\\t+\\t.\\tID=g1\\n'\n",
        encoding="utf-8",
    )
    augustus.chmod(0o755)
    gffread = bin_dir / "gffread"
    gffread.write_text(
        "#!/bin/sh\n"
        'prot=""; cds=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -y) prot="$2"; shift 2;;\n'
        '    -x) cds="$2"; shift 2;;\n'
        "    -g) shift 2;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        '[ -n "$prot" ] && printf \'>g1\\nMKT\\n\' > "$prot"\n'
        '[ -n "$cds" ] && printf \'>g1\\nATGAAAACC\\n\' > "$cds"\n',
        encoding="utf-8",
    )
    gffread.chmod(0o755)
    return bin_dir


def _assert_existing_assembly_blocks_at_review(
    test: unittest.TestCase,
    *,
    code: int,
    stdout: str,
    stderr: str,
    data_dir: Path,
    session_id: str,
    checkpointer: str = "memory",
) -> None:
    test.assertEqual(code, 0)
    test.assertEqual(stderr, "")
    test.assertIn("current_stage: assembly_review", stdout)
    test.assertIn("supervision_state: blocked_human", stdout)
    if checkpointer == "sqlite":
        test.assertIn(
            "resume_note: use `gene-agent review` or `gene-agent resume` to continue",
            stdout,
        )
    else:
        test.assertIn(
            "resume_note: durable cross-process resume requires --checkpointer sqlite",
            stdout,
        )
    run_dir, _request_ref, request_record = _load_assembly_review_request(
        data_dir, session_id
    )
    blocked_report = run_dir / "reports" / "current_status.json"
    test.assertTrue(blocked_report.exists())

    test.assertEqual(request_record["stage"], "assembly_review")
    request = request_record["payload"]
    test.assertIn(":registered_assembly:", request["candidates"][0]["candidate_id"])
    test.assertIsInstance(request["candidates"][0]["primary_output_ref"], dict)
    test.assertFalse(request["auto_decidable"])

    report = json.loads(blocked_report.read_text(encoding="utf-8"))
    test.assertEqual(report["current_stage"], "assembly_review")
    test.assertEqual(report["supervision_state"], "blocked_human")
    test.assertEqual(report["next_action"]["kind"], "review_decision_required")

    artifact_types = _artifact_types(data_dir, session_id)
    test.assertIn("assembly_review.request", artifact_types)
    test.assertIn("assembly_review.review_packet", artifact_types)
    test.assertNotIn("operator_report.blocked_human", artifact_types)
    test.assertTrue((run_dir / "reports" / "assembly_review.md").exists())

    events_path = run_dir / "ledger" / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    test.assertTrue(any(event["event_type"] == "review_requested" for event in events))
    test.assertFalse(any(event["event_type"] == "review_decision" for event in events))


def _write_accept_existing_assembly_decision(
    data_dir: Path, session_id: str, path: Path
) -> Path:
    run_dir, request_ref, request_record = _load_assembly_review_request(
        data_dir, session_id
    )
    request = request_record["payload"]
    candidate = request["candidates"][0]
    decision_path = run_dir / "control" / path.name
    decision_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "stage": request_record["stage"],
                "gate_id": request_record["gate_id"],
                "review_request_id": request_record["review_request_id"],
                "review_request_hash": request_ref.sha256,
                "decision": "accept",
                "candidate_id": candidate["candidate_id"],
                "rejected_candidates": [],
                "notes": ["accepted existing assembly for downstream E2E"],
                "decided_by": "human",
            }
        ),
        encoding="utf-8",
    )
    return decision_path


class EntrypointTests(unittest.TestCase):
    def test_top_level_help_exposes_tui_without_advertising_run_or_ask(self):
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as exit,
        ):
            _parse_args(["--help"])

        self.assertEqual(exit.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("Start and run workflows with 'gene-agent tui'", help_text)
        self.assertRegex(help_text, r"(?m)^\s+tui\s+Start, run, or resume")
        self.assertRegex(help_text, r"(?m)^\s+bootstrap\s+Auxiliary machine")
        self.assertNotRegex(help_text, r"(?m)^\s+run\s+")
        self.assertNotRegex(help_text, r"(?m)^\s+ask\s+")

    def test_removed_run_and_ask_commands_are_rejected(self):
        for command in ("run", "ask"):
            with self.subTest(command=command):
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as exit,
                ):
                    _parse_args([command])

                self.assertEqual(exit.exception.code, 2)
                self.assertIn("invalid choice", stderr.getvalue())

    def test_tui_prompt_example_mentions_target_species(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch("builtins.input", return_value=""),
        ):
            code = _tui_main("data")

        self.assertEqual(code, 1)
        self.assertIn("目标物种", stdout.getvalue())
        self.assertIn("卵鳎", stdout.getvalue())

    def test_tui_request_interrupt_reports_phase(self):
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            code = run_tui("data")

        self.assertEqual(code, 1)
        self.assertIn("Aborted during request entry.", stdout.getvalue())

    def test_tui_resume_session_uses_durable_interactive_review(self):
        with patch(
            "app.entrypoints.review_interactive.run_interactive_review",
            return_value=0,
        ) as review:
            code = run_tui("/tmp/gene-agent-data", resume_session="blocked-session")

        self.assertEqual(code, 0)
        review.assert_called_once_with("blocked-session", "/tmp/gene-agent-data")

    def test_tui_parser_accepts_blocked_session_attachment(self):
        args = _parse_args(
            [
                "tui",
                "--data-dir",
                "out",
                "--resume-session",
                "blocked-session",
            ]
        )

        self.assertEqual(args.command, "tui")
        self.assertEqual(args.resume_session, "blocked-session")

    def test_local_exec_parser_choices_match_run_config_schema(self):
        schema_choices = set(get_args(LocalExecStage))
        cli_choices = set(_LOCAL_EXEC_STAGE_CHOICES)

        self.assertEqual(cli_choices, schema_choices)

    def test_inspect_args_parse_session_id(self):
        args = _parse_args(["inspect", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "inspect")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")

    def test_inspect_args_accept_json_and_template_flags(self):
        args = _parse_args(
            [
                "inspect",
                "--session-id",
                "s1",
                "--data-dir",
                "out",
                "--json",
                "--write-decision-template",
                "decision.json",
            ]
        )

        self.assertTrue(args.json)
        self.assertEqual(args.write_decision_template, "decision.json")

    def test_storage_audit_args_are_read_only_and_json_only(self):
        args = _parse_args(
            [
                "storage-audit",
                "--session-id",
                "s1",
                "--data-dir",
                "out",
                "--format",
                "json",
                "--dry-run-manifest",
            ]
        )

        self.assertEqual(args.command, "storage-audit")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.format, "json")
        self.assertTrue(args.dry_run_manifest)
        self.assertFalse(hasattr(args, "apply"))

    def test_storage_audit_cli_emits_optional_dry_run_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            report = paths.report_path("session_summary.json")
            report.write_text("{}\n", encoding="utf-8")
            work = paths.work_path("assembly", "resume-required.txt")
            work.parent.mkdir(parents=True, exist_ok=True)
            work.write_text("keep\n", encoding="utf-8")
            args = _parse_args(
                [
                    "storage-audit",
                    "--session-id",
                    "s1",
                    "--data-dir",
                    tmp,
                    "--format",
                    "json",
                    "--dry-run-manifest",
                ]
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = _run_storage_audit(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["audit"]["session_id"], "s1")
        self.assertEqual(payload["cleanup_manifest"]["candidate_file_count"], 1)
        self.assertEqual(payload["cleanup_manifest"]["retained_work_file_count"], 1)

    def test_storage_audit_cli_returns_nonzero_on_integrity_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            paths.artifact_index_path.write_text("{not-json}\n", encoding="utf-8")
            args = _parse_args(
                [
                    "storage-audit",
                    "--session-id",
                    "s1",
                    "--data-dir",
                    tmp,
                    "--format",
                    "json",
                ]
            )
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                code = _run_storage_audit(args)

        self.assertEqual(code, 1)
        self.assertIn("storage audit failed", stderr.getvalue())

    def test_review_args_accept_checkpointer_flag(self):
        args = _parse_args(
            [
                "review",
                "--session-id",
                "s1",
                "--data-dir",
                "out",
                "--checkpointer",
                "sqlite",
            ]
        )

        self.assertEqual(args.command, "review")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")
        self.assertEqual(args.checkpointer, "sqlite")

    def test_generate_session_id_uses_daily_sequence_from_v2_run_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            v2_run_dir(data_dir, "gene-20260508-000001")
            v2_run_dir(data_dir, "gene-20260508-000003")
            v2_run_dir(data_dir, "other-session")

            session_id = generate_session_id(
                data_dir, now=datetime(2026, 5, 8, 10, 0, 0)
            )

        self.assertEqual(session_id, "gene-20260508-000004")

    def test_generate_session_id_starts_at_one_for_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = generate_session_id(
                Path(tmp) / "data", now=datetime(2026, 5, 8, 10, 0, 0)
            )

        self.assertEqual(session_id, "gene-20260508-000001")

    def test_build_selection_materializes_route_as_canonical_selection(
        self,
    ):
        selection = _build_assembly_selection(
            assembly=AssemblyRunConfig(assembly_route="ont"),
            shared_parameters=None,
        )

        self.assertIsNotNone(selection)
        self.assertEqual(
            selection.model_dump(mode="json")["primary_method"],
            "assembly.candidate.hifiasm_ont",
        )
        self.assertEqual(selection.model_dump(mode="json")["read_type"], "ont_raw")
        self.assertEqual(selection.model_dump(mode="json")["source"], "run_config")

    def test_structured_run_generates_session_id_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("assets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
                patch(
                    "app.entrypoints.run.generate_session_id",
                    return_value="gene-20260508-000001",
                ),
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id=None,
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("session_id: gene-20260508-000001", stdout)
            self.assertEqual(captured["session_id"], "gene-20260508-000001")
            self.assertEqual(
                graph.invocations[0][1],
                {"configurable": {"thread_id": "gene-20260508-000001"}},
            )
            self.assertTrue(
                (
                    root
                    / "data"
                    / "runs"
                    / "gene-20260508-000001"
                    / "ledger"
                    / "events.jsonl"
                ).exists()
            )

    def test_structured_run_uses_manifest_session_id_when_cli_omits_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
                patch("app.entrypoints.run.generate_session_id") as gen_id,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id=None,
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(captured["session_id"], "s1")
            self.assertIn("session_id: s1", stdout)
            gen_id.assert_not_called()

    def test_structured_run_invokes_graph_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": ["inventory_stage"],
                    "asset_registry_ref": "data/artifacts/s1/assets.json",
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id="s1",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("checkpoint_persistence: sqlite", stdout)
            self.assertIn("supervision_state: completed", stdout)
            self.assertEqual(
                captured,
                {
                    "session_id": "s1",
                    "input_manifest_ref": "control/manifest.yaml",
                    "data_dir": str(SessionPaths(root / "data", "s1").run_dir),
                    "requested_stages": None,
                },
            )
            self.assertEqual(
                graph.invocations[0][1], {"configurable": {"thread_id": "s1"}}
            )
            self.assertTrue(
                (root / "data" / "runs" / "s1" / "ledger" / "events.jsonl").exists()
            )

    def test_structured_run_does_not_refinalize_interactive_resume_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "route_trace": ["assembly_stage"],
                }
            )
            terminal = {
                "current_stage": "done",
                "supervision_state": "completed",
                "route_trace": ["assembly_stage", "done_stage"],
            }

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run._interactive_gate_loop", return_value=terminal
                ),
                patch("app.entrypoints.run.finalize_terminal_state") as finalize,
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id="s1",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        checkpointer="sqlite",
                        interactive_gate=True,
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            finalize.assert_not_called()

    def test_structured_run_resolves_relative_data_dir_before_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )
            captured = {}
            old_cwd = Path.cwd()

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            try:
                os.chdir(root)
                expected = str(SessionPaths(_resolve_data_dir("out"), "s1").run_dir)
                with (
                    patch(
                        "app.entrypoints.run.build_graph", return_value=graph
                    ) as build_graph,
                    patch(
                        "app.entrypoints.run.initial_state",
                        side_effect=fake_initial_state,
                    ),
                ):
                    code, _stdout, stderr = _capture_cli(
                        _run_session,
                        **dict(
                            session_id="s1",
                            manifest=str(manifest),
                            data_dir="out",
                            local_exec=[],
                            run_config=None,
                            checkpointer="memory",
                        ),
                    )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(captured["data_dir"], expected)
            build_graph.assert_called_once_with(backend="memory", data_dir=expected)
            self.assertTrue(
                (root / "out" / "runs" / "s1" / "ledger" / "events.jsonl").exists()
            )

    def test_missing_manifest_returns_before_graph_invocation(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("app.entrypoints.run.build_graph") as build_graph,
        ):
            code, stdout, stderr = _capture_cli(
                _run_session,
                **dict(
                    session_id="s1",
                    manifest="missing.yaml",
                    data_dir=str(Path(tmp) / "data"),
                ),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error: manifest not found", stderr)
        build_graph.assert_not_called()

    def test_missing_manifest_asset_path_reports_inventory_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.fastq"
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        "session_id: s1",
                        "assets:",
                        "  - asset_type: raw_long_reads",
                        f"    paths: [{missing}]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            code, stdout, stderr = _capture_cli(
                _run_session,
                **dict(
                    session_id="s1",
                    manifest=str(manifest),
                    data_dir=str(root / "data"),
                    local_exec=[],
                    run_config=None,
                    checkpointer="memory",
                ),
            )

        self.assertEqual(code, 1)
        self.assertIn("current_stage: inventory", stdout)
        self.assertIn("supervision_state: failed_terminal", stdout)
        self.assertIn("inventory validation failed", stderr)
        self.assertIn("raw_long_reads: missing files", stderr)
        self.assertIn(str(missing), stderr)
        self.assertNotIn("missing asset_registry_ref", stderr)

    def test_blank_manifest_is_rejected_before_path_resolution(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("app.entrypoints.run.build_graph") as build_graph,
        ):
            code, stdout, stderr = _capture_cli(
                _run_session,
                **dict(
                    session_id="s1", manifest="", data_dir=str(Path(tmp) / "data")
                ),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid run request", stderr)
        build_graph.assert_not_called()

    def test_path_shaped_session_id_is_rejected_before_ledger_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with patch("app.entrypoints.run.build_graph") as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id="/tmp/x",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("session_id", stderr)
            self.assertFalse((root / "data" / "ledger").exists())
            build_graph.assert_not_called()

    def test_cli_session_id_mismatch_with_manifest_is_rejected_before_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: from-manifest\nassets: []\n", encoding="utf-8"
            )
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id="from-cli",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("from-manifest", stderr)
            self.assertIn("from-cli", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_cli_session_id_matching_manifest_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: agreed\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id="agreed",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(captured["session_id"], "agreed")

    def test_path_escape_session_id_in_manifest_is_rejected_when_cli_omits_it(self):
        # Adopting manifest session_id must run through EntryRequest's
        # _validate_session_id field validator (model_copy alone would skip
        # it). A path-shaped value like '../escape' would otherwise be used
        # to compose data_dir/ledger/<session_id>.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: ../escape\nassets: []\n", encoding="utf-8")
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **dict(
                        session_id=None,
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("session_id", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_inspect_missing_session_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            v2_run_dir(data_root, "existing")
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="missing", data_dir=str(data_root)),
            )

        self.assertEqual(code, 1)
        self.assertIn("checkpoint_persistence: memory_only", stdout)
        self.assertIn("durable_resume_available: false", stdout)
        self.assertIn("session_not_found", stderr)

    def test_inspect_missing_session_json_returns_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            v2_run_dir(data_root, "existing")
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(
                    session_id="missing", data_dir=str(data_root), json=True
                ),
            )

        self.assertEqual(code, 1)
        summary = json.loads(stdout)
        self.assertEqual(summary["error"], "session_not_found")
        self.assertEqual(summary["next_action"]["kind"], "inspect_unavailable")
        self.assertIn("session_not_found", stderr)

    def test_inspect_existing_session_prints_memory_only_limitation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            run_dir, persisted = _write_assembly_review_request(data_dir, "s1")
            ledger = Ledger(run_dir)
            ledger.create_session(
                "s1", SessionPaths.from_run_dir(run_dir).manifest_path
            )
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="needs_review",
                review_request_artifact_ref=persisted.request_ref,
                review_request_id=persisted.review_request_id,
                auto_decidable=False,
            )
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_dir)),
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("supervision_state: blocked_human", stdout)
        self.assertIn("durable_resume_available: false", stdout)
        self.assertIn("latest_review_request", stdout)

    def test_inspect_blocked_review_prints_next_action_and_writes_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            run_dir, persisted = _write_assembly_review_request(data_dir, "s1")
            ledger = Ledger(run_dir)
            ledger.create_session(
                "s1", SessionPaths.from_run_dir(run_dir).manifest_path
            )
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="needs_review",
                review_request_artifact_ref=persisted.request_ref,
                review_request_id=persisted.review_request_id,
                auto_decidable=False,
            )
            template_path = Path(tmp) / "decision.json"
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(
                    session_id="s1",
                    data_dir=str(data_dir),
                    write_decision_template=str(template_path),
                ),
            )
            template = json.loads(template_path.read_text(encoding="utf-8"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("next_action.kind: review_decision_required", stdout)
            self.assertIn("--write-decision-template", stdout)
            self.assertIn("gene-agent resume", stdout)
            self.assertIn("assembly_review.md", stdout)
            self.assertEqual(template["session_id"], "s1")
            self.assertEqual(template["stage"], "assembly_review")
            self.assertEqual(template["gate_id"], "assembly_review")
            self.assertEqual(template["review_request_id"], persisted.review_request_id)
            self.assertEqual(template["review_request_hash"], persisted.request_hash)
            self.assertEqual(template["decision"], "accept")
            self.assertEqual(template["candidate_id"], "flye")
            self.assertNotIn("final_assembly_ref", template)
            self.assertEqual(
                template["metadata"]["template_guidance"][
                    "candidate_primary_output_refs"
                ]["flye"],
                _artifact_ref_json("flye"),
            )
            self.assertEqual(template["decided_by"], "human")
            self.assertIn(
                "assembly_review_report",
                template["metadata"]["template_guidance"]["report_refs"],
            )

    def test_inspect_json_includes_review_next_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            run_dir, persisted = _write_assembly_review_request(data_dir, "s1")
            ledger = Ledger(run_dir)
            ledger.create_session(
                "s1", SessionPaths.from_run_dir(run_dir).manifest_path
            )
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="needs_review",
                review_request_artifact_ref=persisted.request_ref,
                review_request_id=persisted.review_request_id,
                auto_decidable=False,
            )
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_dir), json=True),
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        summary = json.loads(stdout)
        self.assertEqual(summary["next_action"]["kind"], "review_decision_required")
        self.assertIn("gene-agent resume", summary["next_action"]["resume_command"])
        self.assertTrue(
            any(
                ref.endswith("assembly_review.md")
                for ref in summary["next_action"]["relevant_refs"]
            )
        )

    def test_dotenv_loader_uses_gene_agent_local_env_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "GENE_AGENT_LLM_API_KEY=sk-from-dotenv\nGENE_AGENT_LLM_MODEL=from-dotenv\n",
                encoding="utf-8",
            )
            old_key = os.environ.get("GENE_AGENT_LLM_API_KEY")
            old_model = os.environ.get("GENE_AGENT_LLM_MODEL")
            try:
                os.environ["GENE_AGENT_LLM_API_KEY"] = "sk-existing"
                os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                with patch("os.getcwd", return_value=str(root)):
                    _load_dotenv_without_override()
                self.assertEqual(os.environ["GENE_AGENT_LLM_API_KEY"], "sk-existing")
                self.assertEqual(os.environ["GENE_AGENT_LLM_MODEL"], "from-dotenv")
            finally:
                if old_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = old_key
                if old_model is None:
                    os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                else:
                    os.environ["GENE_AGENT_LLM_MODEL"] = old_model

    def test_main_loads_dotenv_for_resume_subcommand(self):
        from app.entrypoints.run import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "GENE_AGENT_DOTENV_RESUME_PROBE=from-dotenv\n",
                encoding="utf-8",
            )
            old = os.environ.get("GENE_AGENT_DOTENV_RESUME_PROBE")
            os.environ.pop("GENE_AGENT_DOTENV_RESUME_PROBE", None)
            try:
                with patch("os.getcwd", return_value=str(root)):
                    # Resume will exit non-zero (no decision file etc.) — we
                    # only care that .env was loaded before _run_resume runs.
                    main(
                        [
                            "resume",
                            "--session-id",
                            "s1",
                            "--decision-file",
                            str(root / "missing.json"),
                            "--data-dir",
                            str(root / "data"),
                            "--checkpointer",
                            "sqlite",
                        ]
                    )
                self.assertEqual(
                    os.environ.get("GENE_AGENT_DOTENV_RESUME_PROBE"), "from-dotenv"
                )
            finally:
                if old is None:
                    os.environ.pop("GENE_AGENT_DOTENV_RESUME_PROBE", None)
                else:
                    os.environ["GENE_AGENT_DOTENV_RESUME_PROBE"] = old


class RunConfigCLIIntegrationTests(unittest.TestCase):
    def _session_kwargs(self, **overrides) -> dict:
        # data_dir has no safe default — every test must point it at a
        # tempfile.TemporaryDirectory() to avoid spilling sqlite
        # checkpoints into the repo cwd. The base dict therefore
        # leaves it required; the assertion fails fast if a future
        # caller forgets to override it.
        if "data_dir" not in overrides:
            raise AssertionError("_namespace requires an explicit data_dir override")
        base = dict(
            session_id="s1",
            manifest="manifest.yaml",
            local_exec=[],
            run_config=None,
        )
        base.update(overrides)
        return base

    def test_run_config_seeds_local_exec_and_flye_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets: []\n",
                encoding="utf-8",
            )
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["read_qc", "assembly", "assembly_review"],
                        "shared_parameters": {
                            "read_type": "ont_raw",
                        },
                        "read_qc": {
                            "length_filter": {
                                "method": "read_qc.length_filter.seqkit_seq",
                                "approved": False,
                                "thresholds": {},
                            }
                        },
                        "genome_survey": {
                            "survey_parameters": {
                                "kmer_size": 21,
                                "hash_size": "1G",
                                "threads": 4,
                            }
                        },
                        "assembly": {
                            "flye_parameters": {
                                "threads": 4,
                            }
                        },
                        "assembly_review": {
                            "quast": {"threads": 2},
                            "related_species_reference_dir": "/ref/ncbi_metazoa",
                        },
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            state = graph.invocations[0][0]
            stage_outputs = state["stage_outputs"]
            for stage in ("read_qc", "assembly", "assembly_review"):
                self.assertEqual(stage_outputs[stage]["execution_mode"], "local")
            self.assertEqual(
                stage_outputs["read_qc"]["length_filter_request"],
                {
                    "method": "read_qc.length_filter.seqkit_seq",
                    "approved": False,
                    "thresholds": {},
                },
            )
            self.assertEqual(
                stage_outputs["genome_survey"]["survey_parameters"],
                {"kmer_size": 21, "hash_size": "1G", "threads": 4},
            )
            self.assertEqual(
                stage_outputs["assembly"]["flye_parameters"],
                {"threads": 4},
            )
            self.assertEqual(
                stage_outputs["assembly_review"]["quast_parameters"], {"threads": 2}
            )
            self.assertEqual(
                stage_outputs["assembly_review"]["related_species_reference_dir"],
                "/ref/ncbi_metazoa",
            )

    def test_run_config_seeds_hifiasm_ont_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "shared_parameters": {"platform": "ont"},
                        "assembly": {
                            "assembly_method": "hifiasm_ont",
                            "hifiasm_ont_parameters": {"threads": 4},
                        },
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            assembly_out = graph.invocations[0][0]["stage_outputs"]["assembly"]
            self.assertEqual(assembly_out["execution_mode"], "local")
            self.assertEqual(
                assembly_out["assembly_selection"]["primary_method"],
                "assembly.candidate.hifiasm_ont",
            )
            self.assertEqual(assembly_out["assembly_selection"]["read_type"], "ont_raw")
            self.assertEqual(assembly_out["assembly_selection"]["source"], "run_config")
            self.assertNotIn("method_decision", assembly_out)
            self.assertEqual(assembly_out["hifiasm_ont_parameters"], {"threads": 4})

    def test_run_config_explicit_assembly_method_without_read_type_fails_before_graph(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "assembly": {
                            "assembly_method": "hifiasm_ont",
                            "hifiasm_ont_parameters": {"threads": 4},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch("app.entrypoints.run.build_graph") as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn(
                "assembly.assembly_method is set but no read_type could be inferred",
                stderr,
            )
            build_graph.assert_not_called()

    def test_run_config_inferrs_hifiasm_ont_method_from_single_parameter_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "assembly": {
                            "assembly_route": "ont",
                            "hifiasm_ont_parameters": {"threads": 4},
                        },
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            assembly_out = graph.invocations[0][0]["stage_outputs"]["assembly"]
            self.assertEqual(
                assembly_out["assembly_selection"]["primary_method"],
                "assembly.candidate.hifiasm_ont",
            )
            self.assertEqual(assembly_out["assembly_selection"]["read_type"], "ont_raw")
            self.assertEqual(assembly_out["assembly_selection"]["source"], "run_config")
            self.assertNotIn("method_decision", assembly_out)
            self.assertEqual(assembly_out["hifiasm_ont_parameters"], {"threads": 4})

    def test_run_config_inferrs_hifiasm_method_from_single_parameter_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "shared_parameters": {"read_type": "pacbio_hifi"},
                        "assembly": {
                            "hifiasm_parameters": {"threads": 4},
                        },
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            assembly_out = graph.invocations[0][0]["stage_outputs"]["assembly"]
            self.assertEqual(
                assembly_out["assembly_selection"]["primary_method"],
                "assembly.candidate.hifiasm",
            )
            self.assertEqual(
                assembly_out["assembly_selection"]["read_type"], "pacbio_hifi"
            )
            self.assertEqual(assembly_out["assembly_selection"]["source"], "run_config")
            self.assertNotIn("method_decision", assembly_out)
            self.assertEqual(assembly_out["hifiasm_parameters"], {"threads": 4})

    def test_run_session_materializes_storage_contract_before_graph_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_payload = {"local_exec": ["read_qc"]}
            config_path.write_text(json.dumps(config_payload), encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            data_dir = root / "data"
            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(data_dir),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            paths = SessionPaths(data_dir, "s1")
            for directory in (
                paths.run_dir,
                paths.control_dir,
                paths.artifacts_dir,
                paths.work_dir,
                paths.ledger_dir,
                paths.checkpoints_dir,
            ):
                self.assertTrue(directory.exists(), directory)
            self.assertEqual(
                paths.manifest_path.read_text(encoding="utf-8"),
                manifest.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                json.loads(paths.run_config_path.read_text(encoding="utf-8")),
                config_payload,
            )

    def test_materialization_is_safe_for_canonical_source_destinations(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            paths = SessionPaths(data_dir, "s1")
            ensure_run_layout(paths)
            paths.manifest_path.write_text(
                "session_id: s1\nassets: []\n", encoding="utf-8"
            )
            paths.run_config_path.write_text(
                json.dumps({"local_exec": ["read_qc"]}), encoding="utf-8"
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            with patch("app.entrypoints.run.build_graph", return_value=graph):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(paths.manifest_path),
                        data_dir=str(data_dir),
                        run_config=str(paths.run_config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                paths.manifest_path.read_text(encoding="utf-8"),
                "session_id: s1\nassets: []\n",
            )
            self.assertEqual(
                json.loads(paths.run_config_path.read_text(encoding="utf-8")),
                {"local_exec": ["read_qc"]},
            )

    def test_run_session_reports_malformed_manifest_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: [\n", encoding="utf-8")

            with patch("app.entrypoints.run.build_graph") as build_graph_mock:
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertIn("manifest could not be read", stderr)
            build_graph_mock.assert_not_called()

    def test_run_session_invoke_exception_writes_durable_failed_terminal_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            data_dir = root / "data"

            class _ExplodingGraph:
                def invoke(self, state, config):
                    raise RuntimeError("parameter artifact validation exploded")

                def get_state(self, config):
                    return SimpleNamespace(
                        values={
                            "current_stage": "assembly",
                            "route_trace": ["inventory_stage", "assembly_stage"],
                        }
                    )

            with patch(
                "app.entrypoints.run.build_graph", return_value=_ExplodingGraph()
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(data_dir),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("workflow invoke failed: RuntimeError", stderr)
            self.assertNotIn("Traceback", stderr)

            session_dir = SessionPaths(data_dir, "s1").run_dir
            report_path = session_dir / "reports" / "current_status.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["supervision_state"], "failed_terminal")
            self.assertEqual(report["current_stage"], "assembly")
            self.assertIn(
                "parameter artifact validation exploded", report["last_error"]
            )
            self.assertEqual(report["next_action"]["kind"], "failed_terminal")

            events = Ledger(session_dir).read_events("s1")
            terminal = [
                event for event in events if event["event_type"] == "session_terminal"
            ]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["outcome"], "failed")

    def test_run_session_invoke_exception_persists_before_terminal_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            data_dir = root / "data"
            operations = []

            class _ExplodingDurableGraph:
                checkpointer = object()

                def invoke(self, state, config):
                    self.state = {
                        **state,
                        "current_stage": "assembly",
                        "route_trace": ["inventory_stage", "assembly_stage"],
                    }
                    raise RuntimeError("durable stage exploded")

                def get_state(self, config):
                    return SimpleNamespace(values=dict(self.state))

                def update_state(self, config, update):
                    operations.append("update_state")
                    self.state.update(update)

            graph = _ExplodingDurableGraph()

            def compact(checkpointer, config, terminal_state):
                operations.append("compact")
                self.assertEqual(terminal_state["supervision_state"], "failed_terminal")
                self.assertIsNone(terminal_state.get("active_interrupt"))
                return True

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.graph.checkpoint_compaction.compact_terminal_checkpoint",
                    side_effect=compact,
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(data_dir),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(operations, ["update_state", "compact"])
            self.assertEqual(graph.state["supervision_state"], "failed_terminal")
            self.assertIn("workflow invoke failed: RuntimeError", stderr)
            self.assertNotIn("failed to persist/compact", stderr)

            session_dir = SessionPaths(data_dir, "s1").run_dir
            event_types = [
                event["event_type"] for event in Ledger(session_dir).read_events("s1")
            ]
            self.assertEqual(event_types.count("session_terminal"), 1)
            self.assertEqual(event_types.count("terminal_compaction_completed"), 1)
            self.assertNotIn("terminal_compaction_skipped", event_types)

    def test_run_config_seeds_nextdenovo_method_and_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets: []\n",
                encoding="utf-8",
            )
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "shared_parameters": {
                            "read_type": "ont_raw",
                        },
                        "assembly": {"nextdenovo_parameters": {"threads": 4}},
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            assembly_out = graph.invocations[0][0]["stage_outputs"]["assembly"]
            self.assertEqual(assembly_out["execution_mode"], "local")
            self.assertEqual(
                assembly_out["assembly_selection"]["primary_method"],
                "assembly.candidate.nextdenovo",
            )
            self.assertEqual(assembly_out["assembly_selection"]["read_type"], "ont_raw")
            self.assertEqual(assembly_out["assembly_selection"]["source"], "run_config")
            self.assertNotIn("method_decision", assembly_out)
            self.assertEqual(
                assembly_out["nextdenovo_parameters"],
                {"threads": 4},
            )

    def test_run_config_seeds_shared_hints_and_flye_threads_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets: []\n",
                encoding="utf-8",
            )
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "shared_parameters": {
                            "read_type": "ont_raw",
                        },
                        "assembly": {"flye_parameters": {"threads": 4}},
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            state = graph.invocations[0][0]
            self.assertEqual(
                state["shared_parameters"],
                {"read_type": "ont_raw"},
            )
            self.assertEqual(
                state["stage_outputs"]["assembly"]["flye_parameters"], {"threads": 4}
            )

    def test_run_config_local_exec_unions_with_cli_local_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps({"local_exec": ["assembly_review"]}),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        local_exec=["read_qc", "assembly_review"],
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            stage_outputs = graph.invocations[0][0]["stage_outputs"]
            self.assertEqual(set(stage_outputs.keys()), {"read_qc", "assembly_review"})
            for stage in ("read_qc", "assembly_review"):
                self.assertEqual(stage_outputs[stage]["execution_mode"], "local")

    def test_run_config_repeat_annotation_seeds_repeatmasker_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps({"local_exec": ["repeat_annotation"]}),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            repeat = graph.invocations[0][0]["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["execution_mode"], "local")
            self.assertEqual(
                repeat["primary_method"], "repeat_annotation.masking.repeatmasker"
            )

    def test_run_config_gene_annotation_seeds_lineage_or_species_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gene_annotation": {
                            "lineage_or_species_hints": {
                                "busco_lineage": "/db/busco/actinopterygii_odb10",
                                "augustus_species_model": "zebrafish",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            gene = graph.invocations[0][0]["stage_outputs"]["gene_annotation"]
            self.assertEqual(
                gene["lineage_or_species_hints"],
                {
                    "busco_lineage": "/db/busco/actinopterygii_odb10",
                    "augustus_species_model": "zebrafish",
                },
            )

    def test_missing_run_config_file_fails_before_graph_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(root / "missing.json"),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("run-config not found", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_invalid_run_config_fails_before_graph_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps({"local_exec": ["assembly"]}),
                encoding="utf-8",
            )
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("flye_parameters", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_cli_local_exec_assembly_without_flye_parameters_fails_before_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps({"local_exec": ["read_qc"]}),
                encoding="utf-8",
            )
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        local_exec=["assembly"],
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("flye_parameters", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_cli_local_exec_revalidates_method_parameter_alignment_after_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "assembly": {
                            "assembly_method": "flye",
                            "nextdenovo_parameters": {"threads": 4},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        local_exec=["assembly"],
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("method 'flye' requires assembly.flye_parameters", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_cli_local_exec_assembly_without_run_config_fails_before_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        local_exec=["assembly"],
                        run_config=None,
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("flye_parameters", stderr)
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())

    def test_run_config_directory_path_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            directory = root / "config_dir"
            directory.mkdir()
            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                patch("app.entrypoints.run.initial_state") as initial_state,
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    **self._session_kwargs(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(directory),
                    ),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("run-config", stderr)
            self.assertTrue(stderr.startswith("error:"))
            build_graph.assert_not_called()
            initial_state.assert_not_called()
            self.assertFalse((root / "data" / "ledger").exists())


class Phase5RepeatMaskerEntrypointE2ETests(unittest.TestCase):
    def _fake_repeatmasker_bin(self, root: Path, *, fail: bool = False) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "RepeatMasker"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            "import sys\n"
            "if " + repr(fail) + ":\n"
            "    print('fake RepeatMasker failed', file=sys.stderr)\n"
            "    sys.exit(9)\n"
            "assembly = pathlib.Path(sys.argv[-1])\n"
            "assembly.with_name(assembly.name + '.masked').write_text("
            "'>ctg1\\nacgtacgt\\n', encoding='utf-8')\n"
            "assembly.with_name(assembly.name + '.out.gff').write_text("
            "'##gff-version 3\\n"
            "ctg1\\tRepeatMasker\\trepeat_region\\t1\\t8\\t.\\t+\\t.\\t"
            "ID=repeat1\\n', encoding='utf-8')\n"
            "print('fake RepeatMasker ok')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def _write_existing_assembly_inputs(
        self, root: Path, *, session_id: str = "phase5-e2e"
    ) -> tuple[Path, Path, Path]:
        assembly = root / "existing_assembly.fa"
        library = root / "trusted_repeats.fa"
        assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        library.write_text(">repeat1\nACAC\n", encoding="utf-8")
        manifest = root / "manifest.yaml"
        manifest.write_text(
            "\n".join(
                [
                    f"session_id: {session_id}",
                    "sample_id: test-sample",
                    "assets:",
                    "  - asset_type: assembly_fasta_registered",
                    f"    paths: [{assembly}]",
                    "  - asset_type: repeat_library",
                    f"    paths: [{library}]",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_config = root / "run_config.json"
        run_config.write_text(
            json.dumps(
                {
                    "local_exec": ["repeat_annotation"],
                    "requested_stages": [
                        "assembly_review",
                        "repeat_annotation",
                        "gene_annotation",
                        "function_annotation",
                    ],
                }
            ),
            encoding="utf-8",
        )
        return manifest, run_config, root / "out"

    def _run_args(
        self,
        *,
        session_id: str,
        manifest: Path,
        run_config: Path,
        data_dir: Path,
        checkpointer: str = "memory",
    ) -> dict:
        return {
            "session_id": session_id,
            "manifest": str(manifest),
            "data_dir": str(data_dir),
            "local_exec": [],
            "run_config": str(run_config),
            "checkpointer": checkpointer,
        }

    def test_existing_assembly_repeatmasker_backbone_blocks_at_assembly_review_gate(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-e2e"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(
                root, session_id=session_id
            )
            bin_dir = self._fake_repeatmasker_bin(root)
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
            )

            with (
                patch.dict(
                    os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
                ),
                patch(
                    "app.stages.repeat_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
            ):
                code, stdout, stderr = _capture_cli(_run_session, **args)

            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
            )

            run_dir = SessionPaths(data_dir, session_id).run_dir
            repeat_bundle = (
                run_dir
                / "artifacts"
                / "repeat_annotation"
                / "repeat_annotation_bundle.json"
            )
            gene_bundle = (
                run_dir
                / "artifacts"
                / "gene_annotation"
                / "gene_annotation_bundle.json"
            )
            function_bundle = (
                run_dir
                / "artifacts"
                / "function_annotation"
                / "function_annotation_bundle.json"
            )
            self.assertFalse(repeat_bundle.exists())
            self.assertFalse(gene_bundle.exists())
            self.assertFalse(function_bundle.exists())

            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("repeat_annotation.softmasked_genome", artifact_types)
            self.assertNotIn("repeat_annotation.gff3", artifact_types)
            self.assertNotIn("gene_annotation.bundle", artifact_types)
            self.assertNotIn("operator_report.failed_terminal", artifact_types)

    def test_repeatmasker_backbone_resumes_to_function_provenance_gate(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-e2e-resume"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(
                root, session_id=session_id
            )
            bin_dir = self._fake_repeatmasker_bin(root)
            _add_gene_method_bins(bin_dir)
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
                checkpointer="sqlite",
            )

            with (
                patch.dict(
                    os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
                ),
                patch(
                    "app.stages.repeat_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.gene_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.gene_annotation.nodes._lineage_or_species_hints",
                    return_value={"augustus_species_model": "test_species"},
                ),
            ):
                code, stdout, stderr = _capture_cli(_run_session, **args)

                _assert_existing_assembly_blocks_at_review(
                    self,
                    code=code,
                    stdout=stdout,
                    stderr=stderr,
                    data_dir=data_dir,
                    session_id=session_id,
                    checkpointer="sqlite",
                )

                decision = _write_accept_existing_assembly_decision(
                    data_dir, session_id, root / "decision.json"
                )
                resume_code, resume_stdout, resume_stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id=session_id,
                        decision_file=str(decision),
                        data_dir=str(data_dir),
                        checkpointer="sqlite",
                        max_blocked_seconds=None,
                    ),
                )

            self.assertEqual(resume_code, 1)
            self.assertIn("current_stage: function_annotation", resume_stdout)
            self.assertIn("supervision_state: failed_terminal", resume_stdout)
            self.assertIn("database_provenance", resume_stderr)

            run_dir = SessionPaths(data_dir, session_id).run_dir
            repeat_bundle = (
                run_dir
                / "artifacts"
                / "repeat_annotation"
                / "repeat_annotation_bundle.json"
            )
            gene_bundle = (
                run_dir
                / "artifacts"
                / "gene_annotation"
                / "gene_annotation_bundle.json"
            )
            function_bundle = (
                run_dir
                / "artifacts"
                / "function_annotation"
                / "function_annotation_bundle.json"
            )
            self.assertTrue(repeat_bundle.exists())
            self.assertTrue(gene_bundle.exists())
            self.assertFalse(function_bundle.exists())

            repeat = json.loads(repeat_bundle.read_text(encoding="utf-8"))
            gene = json.loads(gene_bundle.read_text(encoding="utf-8"))
            self.assertEqual(repeat["status"], "executed")
            self.assertEqual(gene["genome_input_ref"], repeat["softmasked_genome_ref"])

            artifact_types = _artifact_types(data_dir, session_id)
            self.assertIn("repeat_annotation.softmasked_genome", artifact_types)
            self.assertIn("repeat_annotation.gff3", artifact_types)
            self.assertIn("gene_annotation.bundle", artifact_types)
            self.assertNotIn("operator_report.failed_terminal", artifact_types)
            self.assertTrue((run_dir / "reports" / "current_status.json").exists())

    def test_existing_assembly_interactive_review_reports_missing_function_provenance(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase6-intake-sqlite-smoke"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(
                root, session_id=session_id
            )
            bin_dir = self._fake_repeatmasker_bin(root)
            _add_gene_method_bins(bin_dir)
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
                checkpointer="sqlite",
            )

            with (
                patch.dict(
                    os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
                ),
                patch(
                    "app.stages.repeat_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.gene_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.gene_annotation.nodes._lineage_or_species_hints",
                    return_value={"augustus_species_model": "test_species"},
                ),
            ):
                code, stdout, stderr = _capture_cli(_run_session, **args)
                _assert_existing_assembly_blocks_at_review(
                    self,
                    code=code,
                    stdout=stdout,
                    stderr=stderr,
                    data_dir=data_dir,
                    session_id=session_id,
                    checkpointer="sqlite",
                )

                inputs = iter(["accept", "", "y"])
                with (
                    patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False),
                    patch(
                        "builtins.input",
                        side_effect=lambda _: next(inputs),
                    ),
                ):
                    review_code, review_stdout, review_stderr = _capture_cli(
                        run_interactive_review,
                        session_id,
                        str(data_dir),
                    )

            self.assertEqual(review_code, 1)
            self.assertNotIn("annotation_intake_plan:", review_stdout)
            self.assertIn("resume annotation_intake failed", review_stderr)
            self.assertIn("database_provenance", review_stderr)

            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("annotation.intake_plan", artifact_types)

            events = Ledger(SessionPaths(data_dir, session_id).run_dir).read_events(
                session_id
            )
            failed = [
                event for event in events if event["event_type"] == "resume_failed"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["phase"], "annotation_intake")
            self.assertFalse(
                any(
                    event["event_type"] == "annotation_intake_planned"
                    for event in events
                )
            )

    def test_repeatmasker_preflight_block_stops_before_command_and_gene_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-preflight-block"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(
                root, session_id=session_id
            )
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
            )

            with patch(
                "app.execution.env_dispatch.preflight",
                return_value=PreflightResult(
                    status="missing_tool",
                    env_target="env_geno_exec",
                    missing_tools=["RepeatMasker"],
                    detail="missing tools in env_geno_exec: RepeatMasker",
                ),
            ) as preflight:
                code, stdout, stderr = _capture_cli(_run_session, **args)

            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
            )
            preflight.assert_not_called()

            run_dir = SessionPaths(data_dir, session_id).run_dir
            repeat_work = run_dir / "work" / "repeat_annotation" / "repeatmasker"
            gene_bundle = (
                run_dir
                / "artifacts"
                / "gene_annotation"
                / "gene_annotation_bundle.json"
            )
            self.assertFalse(repeat_work.exists())
            self.assertFalse(gene_bundle.exists())
            events_path = run_dir / "ledger" / "events.jsonl"
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            blocks = [
                event
                for event in events
                if event["event_type"] == "env_preflight_blocked"
            ]
            self.assertEqual(blocks, [])
            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("operator_report.failed_terminal", artifact_types)

    def test_repeatmasker_failure_stops_before_gene_annotation_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-repeatmasker-fails"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(
                root, session_id=session_id
            )
            bin_dir = self._fake_repeatmasker_bin(root, fail=True)
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
            )

            with (
                patch.dict(
                    os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
                ),
                patch(
                    "app.stages.repeat_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
            ):
                code, stdout, stderr = _capture_cli(_run_session, **args)

            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
            )
            run_dir = SessionPaths(data_dir, session_id).run_dir
            repeat_bundle = (
                run_dir
                / "artifacts"
                / "repeat_annotation"
                / "repeat_annotation_bundle.json"
            )
            gene_bundle = (
                run_dir
                / "artifacts"
                / "gene_annotation"
                / "gene_annotation_bundle.json"
            )
            self.assertFalse(repeat_bundle.exists())
            self.assertFalse(gene_bundle.exists())
            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("command.stderr", artifact_types)


class Phase5ProvidedAnnotationEntrypointE2ETests(unittest.TestCase):
    def _write_provided_annotation_inputs(
        self,
        root: Path,
        *,
        session_id: str = "phase5-provided-ann-e2e",
        include_provenance: bool = False,
    ) -> tuple[Path, Path, Path]:
        assembly = root / "existing_assembly.fa"
        gene_models = root / "gene_models.gff3"
        proteins = root / "predicted_proteins.fasta"
        transcripts = root / "transcripts.fa"
        database_fasta = root / "uniprot.fasta"
        assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        gene_models.write_text(
            "##gff-version 3\nctg1\tprediction\tgene\t1\t8\t.\t+\t.\tID=gene1\n",
            encoding="utf-8",
        )
        proteins.write_text(">prot1\nMACGT\n", encoding="utf-8")
        transcripts.write_text(">tx1\nACGTACGT\n", encoding="utf-8")
        database_fasta.write_text(">sp|P12345\nMACGT\n", encoding="utf-8")
        manifest = root / "manifest.yaml"
        manifest.write_text(
            "\n".join(
                [
                    f"session_id: {session_id}",
                    "sample_id: test-sample",
                    "assets:",
                    "  - asset_type: assembly_fasta_registered",
                    f"    paths: [{assembly}]",
                    "  - asset_type: gene_models",
                    f"    paths: [{gene_models}]",
                    "  - asset_type: predicted_proteins",
                    f"    paths: [{proteins}]",
                    "  - asset_type: transcripts_fasta",
                    f"    paths: [{transcripts}]",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_config = root / "run_config.json"
        run_config_payload = {
            "local_exec": [],
            "requested_stages": [
                "assembly_review",
                "repeat_annotation",
                "gene_annotation",
                "function_annotation",
            ],
        }
        if include_provenance:
            run_config_payload["function_annotation"] = {
                "database_provenance": [
                    {
                        "method_name": "function_annotation.candidate.diamond",
                        "database_name": "UniProt",
                        "database_version": "2026_02",
                        "snapshot_date": "2026-05-25",
                        "database_source_ref": str(database_fasta),
                        "descriptor_version": "diamond.yaml",
                        "input_proteins_ref": str(proteins),
                    }
                ]
            }
        run_config.write_text(json.dumps(run_config_payload), encoding="utf-8")
        return manifest, run_config, root / "out"

    def _run_args(
        self,
        *,
        session_id: str,
        manifest: Path,
        run_config: Path,
        data_dir: Path,
        checkpointer: str = "memory",
    ) -> dict:
        return {
            "session_id": session_id,
            "manifest": str(manifest),
            "data_dir": str(data_dir),
            "local_exec": [],
            "run_config": str(run_config),
            "checkpointer": checkpointer,
        }

    def test_existing_assembly_provided_annotation_blocks_at_assembly_review_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-provided-ann-e2e"
            manifest, run_config, data_dir = self._write_provided_annotation_inputs(
                root, session_id=session_id
            )
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
            )

            code, stdout, stderr = _capture_cli(_run_session, **args)

            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
            )

            run_dir = SessionPaths(data_dir, session_id).run_dir
            gene_bundle_path = (
                run_dir
                / "artifacts"
                / "gene_annotation"
                / "gene_annotation_bundle.json"
            )
            function_bundle_path = (
                run_dir
                / "artifacts"
                / "function_annotation"
                / "function_annotation_bundle.json"
            )
            self.assertFalse(gene_bundle_path.exists())
            self.assertFalse(function_bundle_path.exists())

            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("gene_annotation.provided_gene_models", artifact_types)
            self.assertNotIn(
                "gene_annotation.provided_predicted_proteins", artifact_types
            )
            self.assertNotIn(
                "gene_annotation.provided_transcripts_fasta", artifact_types
            )
            self.assertNotIn("gene_annotation.bundle", artifact_types)
            self.assertNotIn("operator_report.failed_terminal", artifact_types)

    def test_provided_annotation_still_blocks_at_assembly_review_gate(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-function-provenance-e2e"
            manifest, run_config, data_dir = self._write_provided_annotation_inputs(
                root,
                session_id=session_id,
                include_provenance=True,
            )
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
            )

            code, stdout, stderr = _capture_cli(_run_session, **args)

            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
            )

            run_dir = SessionPaths(data_dir, session_id).run_dir
            function_bundle_path = (
                run_dir
                / "artifacts"
                / "function_annotation"
                / "function_annotation_bundle.json"
            )
            final_report_path = run_dir / "reports" / "session_summary.json"
            self.assertFalse(function_bundle_path.exists())
            self.assertFalse(final_report_path.exists())
            artifact_types = _artifact_types(data_dir, session_id)
            self.assertNotIn("function_annotation.plan", artifact_types)
            self.assertNotIn("function_annotation.bundle", artifact_types)
            self.assertNotIn("final_report.review_decisions", artifact_types)

            events_path = (
                SessionPaths(data_dir, session_id).run_dir / "ledger" / "events.jsonl"
            )
            artifact_events = [
                json.loads(line)["artifact"]["artifact_type"]
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["event_type"] == "artifact_registered"
            ]
            self.assertNotIn("function_annotation.plan", artifact_events)
            self.assertNotIn("function_annotation.bundle", artifact_events)
            self.assertNotIn("final_report.review_decisions", artifact_events)

    def test_provided_annotation_with_provenance_resumes_and_completes(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-function-provenance-resume"
            manifest, run_config, data_dir = self._write_provided_annotation_inputs(
                root,
                session_id=session_id,
                include_provenance=True,
            )
            args = self._run_args(
                session_id=session_id,
                manifest=manifest,
                run_config=run_config,
                data_dir=data_dir,
                checkpointer="sqlite",
            )

            code, stdout, stderr = _capture_cli(_run_session, **args)
            _assert_existing_assembly_blocks_at_review(
                self,
                code=code,
                stdout=stdout,
                stderr=stderr,
                data_dir=data_dir,
                session_id=session_id,
                checkpointer="sqlite",
            )

            decision = _write_accept_existing_assembly_decision(
                data_dir, session_id, root / "decision.json"
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_diamond = fake_bin / "diamond"
            fake_diamond.write_text(
                "#!/bin/sh\n"
                "out=''\n"
                "while [ $# -gt 0 ]; do\n"
                '  if [ "$1" = \'--out\' ]; then out="$2"; shift 2; continue; fi\n'
                "  shift\n"
                "done\n"
                'if [ -n "$out" ]; then\n'
                "  printf 'prot1\\tsp|P12345\\t100.0\\t5\\t0\\t0\\t"
                '1\\t5\\t1\\t5\\t0.0\\t10\\n\' > "$out"\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_diamond.chmod(0o755)
            fake_path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            with (
                patch(
                    "app.stages.function_annotation.nodes.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch.dict(os.environ, {"PATH": fake_path}),
            ):
                resume_code, resume_stdout, resume_stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id=session_id,
                        decision_file=str(decision),
                        data_dir=str(data_dir),
                        checkpointer="sqlite",
                        max_blocked_seconds=None,
                    ),
                )

            self.assertEqual(resume_code, 0, resume_stderr)
            self.assertIn("current_stage: done", resume_stdout)
            self.assertIn("supervision_state: completed", resume_stdout)

            proteins_file = root / "predicted_proteins.fasta"
            run_dir = SessionPaths(data_dir, session_id).run_dir
            function_bundle_path = (
                run_dir
                / "artifacts"
                / "function_annotation"
                / "function_annotation_bundle.json"
            )
            final_report_path = run_dir / "reports" / "session_summary.json"
            self.assertTrue(function_bundle_path.exists())
            self.assertTrue(final_report_path.exists())

            bundle = json.loads(function_bundle_path.read_text(encoding="utf-8"))
            canonical_proteins = str(proteins_file.resolve())
            self.assertEqual(bundle["proteins_fasta_ref"], canonical_proteins)
            self.assertEqual(
                bundle["database_provenance"][0]["input_proteins_ref"],
                canonical_proteins,
            )
            self.assertEqual(
                bundle["database_provenance"][0]["database_source_ref"],
                str(root / "uniprot.fasta"),
            )

            artifact_types = _artifact_types(data_dir, session_id)
            self.assertIn("function_annotation.plan", artifact_types)
            self.assertIn("function_annotation.bundle", artifact_types)
            self.assertNotIn("final_report.review_decisions", artifact_types)


class InteractiveGateLoopTests(unittest.TestCase):
    def test_annotation_intake_cancel_does_not_resume_or_write_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            session_id = "s1"
            run_dir, persisted = _write_assembly_review_request(data_root, session_id)
            paths = SessionPaths.from_run_dir(run_dir)
            Ledger(run_dir).create_session(session_id, paths.manifest_path)
            state = {
                "session_id": session_id,
                "data_dir": str(run_dir),
                "control_state": "running",
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "requested_stages": ["read_qc", "assembly", "assembly_review"],
                "input_manifest_ref": paths.to_ref(paths.manifest_path),
                "active_interrupt": persisted.compact_interrupt(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="needs_review",
                ),
                "stage_outputs": {
                    "assembly_review": {
                        "status": "blocked_human",
                        "candidates": [
                            {
                                "candidate_id": "flye",
                                "method_name": "assembly.candidate.flye",
                                "source": "assembly",
                                "status": "executed",
                                "bundle_ref": _artifact_ref_json(
                                    "flye",
                                    artifact_type="draft_assembly.candidate",
                                    suffix=".json",
                                ),
                                "primary_output_ref": _artifact_ref_json("flye"),
                            }
                        ],
                        "review_request_artifact_ref": persisted.request_ref.model_dump(
                            mode="json"
                        ),
                    }
                },
                "review_notes": [],
                "route_trace": [],
                "skip_reasons": {},
            }
            graph = _FakeResumeGraph(
                state, result={"supervision_state": "blocked_human"}
            )
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash=persisted.request_hash,
                decision="accept",
                candidate_id="flye",
            )
            raw_decision = request.model_dump(mode="json")

            with (
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision",
                    return_value=(request, raw_decision),
                ),
                patch(
                    "app.review.annotation_intake.collect_annotation_intake_selection",
                    side_effect=RuntimeError("annotation intake cancelled"),
                ) as intake_prompt,
                patch(
                    "app.control.resume_transaction.resume_review_state"
                ) as resume_mock,
            ):
                result = _interactive_gate_loop(
                    graph,
                    {"configurable": {"thread_id": session_id}},
                    {"supervision_state": "blocked_human"},
                    session_id,
                    str(run_dir),
                )

            self.assertEqual(result["supervision_state"], "blocked_human")
            intake_prompt.assert_called_once()
            resume_mock.assert_not_called()
            self.assertEqual(graph.updated, [])
            decision_path = (
                run_dir
                / "artifacts"
                / "reviews"
                / persisted.review_request_id
                / "decision.json"
            )
            self.assertFalse(decision_path.exists())
            events = Ledger(run_dir).read_events(session_id)
            self.assertFalse(
                any(event["event_type"] == "review_decision" for event in events)
            )


class ResumeCLITests(unittest.TestCase):
    def _write_decision(self, path: Path, **overrides) -> Path:
        payload = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "accept",
            "candidate_id": "flye",
            "notes": ["test"],
        }
        payload.update(overrides)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_parser_accepts_resume_subcommand(self):
        args = _parse_args(
            [
                "resume",
                "--session-id",
                "s1",
                "--decision-file",
                "decision.json",
                "--data-dir",
                "out",
            ]
        )
        self.assertEqual(args.command, "resume")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.decision_file, "decision.json")
        self.assertEqual(args.data_dir, "out")
        # Default is None at the parser level; backend resolution falls
        # through to GENE_AGENT_CHECKPOINTER, then DEFAULT_CHECKPOINTER_BACKEND.
        self.assertIsNone(args.checkpointer)

    def test_resume_rejects_memory_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = self._write_decision(Path(tmp) / "decision.json")
            code, stdout, stderr = _capture_cli(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision),
                    data_dir=str(Path(tmp) / "data"),
                    checkpointer="memory",
                ),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("durable resume requires --checkpointer sqlite", stderr)

    def test_resume_rejects_decision_session_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = self._write_decision(
                Path(tmp) / "decision.json", session_id="other"
            )
            code, stdout, stderr = _capture_cli(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision),
                    data_dir=str(Path(tmp) / "data"),
                    checkpointer="sqlite",
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("'other'", stderr)
        self.assertIn("'s1'", stderr)

    def test_resume_rejects_missing_decision_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = _capture_cli(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(Path(tmp) / "missing.json"),
                    data_dir=str(Path(tmp) / "data"),
                    checkpointer="sqlite",
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("decision file not found", stderr)

    def test_resume_rejects_invalid_decision_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = Path(tmp) / "decision.json"
            decision.write_text("{not json", encoding="utf-8")
            code, stdout, stderr = _capture_cli(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision),
                    data_dir=str(Path(tmp) / "data"),
                    checkpointer="sqlite",
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", stderr)

    def test_resume_rejects_missing_checkpoint_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = self._write_decision(Path(tmp) / "decision.json")
            data_dir = Path(tmp) / "data"
            code, stdout, stderr = _capture_cli(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision),
                    data_dir=str(data_dir),
                    checkpointer="sqlite",
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("no durable checkpoint database", stderr)
        self.assertIn("--data-dir", stderr)
        self.assertFalse(data_dir.exists())
        self.assertFalse((data_dir / "checkpoints").exists())
        self.assertFalse((data_dir / "ledger").exists())

    def test_resume_started_records_resolved_session_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            session_id = "s1"
            run_dir, persisted = _write_assembly_review_request(data_root, session_id)
            paths = SessionPaths.from_run_dir(run_dir)
            paths.checkpoint_db_path.write_text("", encoding="utf-8")
            Ledger(run_dir).create_session(session_id, paths.manifest_path)
            decision = self._write_decision(
                paths.control_dir / "decision.json",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash=persisted.request_hash,
            )
            state = {
                "session_id": session_id,
                "data_dir": str(run_dir),
                "control_state": "running",
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "input_manifest_ref": paths.to_ref(paths.manifest_path),
                "active_interrupt": persisted.compact_interrupt(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="needs_review",
                ),
                "review_notes": [],
                "route_trace": [],
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "skip_reasons": {},
            }
            graph = _FakeResumeGraph(state)

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={
                        "current_stage": "done",
                        "supervision_state": "completed",
                    },
                ),
                patch(
                    "app.control.resume_transaction.maybe_collect_annotation_intake",
                    return_value=None,
                ),
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id=session_id,
                        decision_file=str(decision),
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                        max_blocked_seconds=None,
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            events = Ledger(run_dir).read_events(session_id)
            started = [
                event for event in events if event["event_type"] == "resume_started"
            ]
            self.assertEqual(started[0]["data_dir"], str(run_dir))

    def test_resume_decision_file_records_recommended_intake_without_prompting(self):
        # A continue-downstream (assembly_review accept) decision-file resume is
        # non-interactive but must still record the RECOMMENDED annotation intake
        # plan that the gene_annotation chain requires at entry. It must not call
        # the interactive prompt.
        from app.storage.artifacts import read_artifact_records

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            session_id = "s1"
            run_dir, persisted = _write_assembly_review_request(data_root, session_id)
            paths = SessionPaths.from_run_dir(run_dir)
            paths.checkpoint_db_path.write_text("", encoding="utf-8")

            manifest_ref = paths.manifest_path
            manifest_ref.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(root / "reads.fastq")],
                                "platform": "ont",
                                "format": "fastq",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            Ledger(run_dir).create_session(session_id, manifest_ref)
            decision = self._write_decision(
                paths.control_dir / "decision.json",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash=persisted.request_hash,
            )
            state = {
                "session_id": session_id,
                "data_dir": str(run_dir),
                "control_state": "running",
                "input_manifest_ref": paths.to_ref(manifest_ref),
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "requested_stages": ["read_qc", "assembly", "assembly_review"],
                "active_interrupt": persisted.compact_interrupt(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="needs_review",
                ),
                "review_notes": [],
                "route_trace": [],
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "skip_reasons": {},
            }
            graph = _FakeResumeGraph(state)

            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={
                        "current_stage": "done",
                        "supervision_state": "completed",
                    },
                ),
                patch(
                    "app.review.annotation_intake.prompt_annotation_intake_plan"
                ) as intake_prompt,
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id=session_id,
                        decision_file=str(decision),
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                        max_blocked_seconds=None,
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            intake_prompt.assert_not_called()
            artifact_types = [
                record["artifact_type"]
                for record in read_artifact_records(run_dir, session_id)
            ]
            self.assertIn("annotation.intake_plan", artifact_types)
            self.assertEqual(len(graph.updated), 1)


class CheckpointerBackendResolutionTests(unittest.TestCase):
    def test_explicit_arg_wins_over_env_var(self):
        ns = argparse.Namespace(checkpointer="sqlite")
        with patch.dict(os.environ, {"GENE_AGENT_CHECKPOINTER": "memory"}):
            self.assertEqual(_resolve_checkpointer_backend(ns), "sqlite")

    def test_env_var_wins_when_arg_is_none(self):
        ns = argparse.Namespace(checkpointer=None)
        with patch.dict(os.environ, {"GENE_AGENT_CHECKPOINTER": "memory"}):
            self.assertEqual(_resolve_checkpointer_backend(ns), "memory")

    def test_default_when_neither_set(self):
        ns = argparse.Namespace(checkpointer=None)
        original = os.environ.pop("GENE_AGENT_CHECKPOINTER", None)
        try:
            self.assertEqual(_resolve_checkpointer_backend(ns), "sqlite")
        finally:
            if original is not None:
                os.environ["GENE_AGENT_CHECKPOINTER"] = original

    def test_missing_attr_falls_through(self):
        ns = argparse.Namespace()
        with patch.dict(os.environ, {"GENE_AGENT_CHECKPOINTER": "memory"}):
            self.assertEqual(_resolve_checkpointer_backend(ns), "memory")


def _capture_cli(func, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
