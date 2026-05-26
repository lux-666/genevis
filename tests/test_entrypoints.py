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
from typing import get_args
from unittest.mock import patch

from app.execution.artifacts import append_artifact_records
from app.execution.env_resolver import PreflightResult
from app.entrypoints.run import _LOCAL_EXEC_STAGE_CHOICES, _load_dotenv_without_override, _parse_args, _resolve_checkpointer_backend, _resolve_data_dir, _run_ask, _run_inspect, _run_resume, _run_session, generate_session_id
from app.schemas.artifacts import ArtifactMeta
from app.schemas.entry import EntryRequest
from app.schemas.run_config import LocalExecStage


class _FakeGraph:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.invocations: list[tuple[dict, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return self.result


def _write_assembly_review_request(data_dir: Path, session_id: str) -> Path:
    request_dir = data_dir / "artifacts" / "assembly_review" / session_id
    request_dir.mkdir(parents=True, exist_ok=True)
    request_ref = request_dir / "assembly_review_request.json"
    request_ref.write_text(
        json.dumps(
            {
                "stage": "assembly_review",
                "reason": "needs_review",
                "candidates": [{"candidate_id": "flye", "final_assembly_ref": "draft.fasta"}],
                "signals": [],
                "decisions_allowed": ["accept", "stop_human_review"],
                "auto_decidable": False,
                "report_refs": {
                    "assembly_review_report": str(request_dir / "reports" / "assembly_review_report.md"),
                    "assembly_review_report_data": str(request_dir / "reports" / "assembly_review_report.json"),
                },
                "candidate_comparison_context": {
                    "candidate_ids": ["flye"],
                    "candidate_final_assembly_refs": {"flye": "draft.fasta"},
                },
                "review_request_artifact_ref": str(request_ref),
            }
        ),
        encoding="utf-8",
    )
    append_artifact_records(
        data_dir,
        session_id,
        [
            ArtifactMeta.from_file(
                request_ref,
                stage="assembly_review",
                method_name="assembly_review.contract",
                artifact_type="assembly_review.request",
            )
        ],
    )
    return request_ref


class EntrypointTests(unittest.TestCase):
    def _ask_namespace(self, **overrides):
        base = dict(
            request_text="use manifest",
            session_id=None,
            data_dir=None,
            local_exec=[],
            run_config=None,
            checkpointer=None,
            ask_plan=None,
            clarification_response=None,
            write_clarification_template=None,
            json=False,
            non_interactive=False,
            resume_session=None,
            set_params=[],
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_stages_flag_is_not_exposed_until_routing_semantics_exist(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            _parse_args(
                [
                    "run",
                    "--session-id",
                    "s1",
                    "--manifest",
                    "manifest.yaml",
                    "--stages",
                    "inventory",
                ]
            )
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_ask_args_parse_minimal_defaults(self):
        args = _parse_args(["ask", "use inputs/manifest.yaml to start s1", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "ask")
        self.assertEqual(args.request_text, "use inputs/manifest.yaml to start s1")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")

    def test_local_exec_parser_choices_match_run_config_schema(self):
        schema_choices = set(get_args(LocalExecStage))
        cli_choices = set(_LOCAL_EXEC_STAGE_CHOICES)

        self.assertEqual(cli_choices, schema_choices)

    def test_ask_args_accept_execution_flags(self):
        args = _parse_args([
            "ask",
            "run reads",
            "--local-exec",
            "read_qc",
            "--local-exec",
            "assembly",
            "--run-config",
            "run_config.json",
            "--checkpointer",
            "sqlite",
            "--ask-plan",
            "plan.json",
            "--clarification-response",
            "response.json",
            "--json",
        ])

        self.assertEqual(args.command, "ask")
        self.assertEqual(args.local_exec, ["read_qc", "assembly"])
        self.assertEqual(args.run_config, "run_config.json")
        self.assertEqual(args.checkpointer, "sqlite")
        self.assertEqual(args.ask_plan, "plan.json")
        self.assertEqual(args.clarification_response, "response.json")
        self.assertTrue(args.json)

    def test_inspect_args_parse_session_id(self):
        args = _parse_args(["inspect", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "inspect")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")

    def test_inspect_args_accept_json_and_template_flags(self):
        args = _parse_args([
            "inspect",
            "--session-id",
            "s1",
            "--data-dir",
            "out",
            "--json",
            "--write-decision-template",
            "decision.json",
        ])

        self.assertTrue(args.json)
        self.assertEqual(args.write_decision_template, "decision.json")

    def test_run_args_can_omit_session_id(self):
        args = _parse_args(["run", "--manifest", "manifest.yaml"])

        self.assertIsNone(args.session_id)
        self.assertEqual(args.manifest, "manifest.yaml")

    def test_generate_session_id_uses_daily_sequence_from_ledger_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            ledger = data_dir / "ledger"
            (ledger / "gene-20260508-000001").mkdir(parents=True)
            (ledger / "gene-20260508-000003").mkdir()
            (ledger / "other-session").mkdir()

            session_id = generate_session_id(data_dir, now=datetime(2026, 5, 8, 10, 0, 0))

        self.assertEqual(session_id, "gene-20260508-000004")

    def test_generate_session_id_starts_at_one_for_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = generate_session_id(Path(tmp) / "data", now=datetime(2026, 5, 8, 10, 0, 0))

        self.assertEqual(session_id, "gene-20260508-000001")

    def test_structured_run_generates_session_id_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("assets: []\n", encoding="utf-8")
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ), patch("app.entrypoints.run.generate_session_id", return_value="gene-20260508-000001"):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(session_id=None, manifest=str(manifest), data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("session_id: gene-20260508-000001", stdout)
            self.assertEqual(captured["session_id"], "gene-20260508-000001")
            self.assertEqual(graph.invocations[0][1], {"configurable": {"thread_id": "gene-20260508-000001"}})
            self.assertTrue((root / "data" / "ledger" / "gene-20260508-000001" / "session.json").exists())

    def test_structured_run_uses_manifest_session_id_when_cli_omits_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ), patch("app.entrypoints.run.generate_session_id") as gen_id:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(session_id=None, manifest=str(manifest), data_dir=str(root / "data")),
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

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(
                        session_id="s1",
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("checkpoint_persistence: sqlite", stdout)
            self.assertIn(
                "resume_note: pass --decision-file to `gene-agent resume`",
                stdout,
            )
            self.assertEqual(
                captured,
                {
                    "session_id": "s1",
                    "input_manifest_ref": str(manifest),
                    "data_dir": _resolve_data_dir(str(root / "data")),
                },
            )
            self.assertEqual(graph.invocations[0][1], {"configurable": {"thread_id": "s1"}})
            self.assertTrue((root / "data" / "ledger" / "s1" / "session.json").exists())

    def test_structured_run_resolves_relative_data_dir_before_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}
            old_cwd = Path.cwd()

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            try:
                os.chdir(root)
                expected = _resolve_data_dir("out")
                with patch("app.entrypoints.run.build_graph", return_value=graph) as build_graph, patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ):
                    code, _stdout, stderr = _capture_cli(
                        _run_session,
                        argparse.Namespace(
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
            self.assertTrue((root / "out" / "ledger" / "s1" / "session.json").exists())

    def test_missing_manifest_returns_before_graph_invocation(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.entrypoints.run.build_graph") as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_session,
                argparse.Namespace(session_id="s1", manifest="missing.yaml", data_dir=str(Path(tmp) / "data")),
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
                argparse.Namespace(
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
        with tempfile.TemporaryDirectory() as tmp, patch("app.entrypoints.run.build_graph") as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_session,
                argparse.Namespace(session_id="s1", manifest="", data_dir=str(Path(tmp) / "data")),
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
                    argparse.Namespace(session_id="/tmp/x", manifest=str(manifest), data_dir=str(root / "data")),
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
            manifest.write_text("session_id: from-manifest\nassets: []\n", encoding="utf-8")
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(
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
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(
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
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(
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

    def test_ask_generates_default_session_id_for_parser_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.entrypoints.intent.parse_request_with_llm", side_effect=ValueError("stop")) as parse_request, patch(
                "app.entrypoints.run.generate_session_id", return_value="gene-20260508-000001"
            ):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text="use manifest", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("stop", stderr)
            parse_request.assert_called_once_with(
                "use manifest",
                defaults={"session_id": "gene-20260508-000001", "data_dir": str(root / "data")},
            )

    def test_ask_without_run_config_emits_clarification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", manifest=str(manifest), data_dir=str(root / "data"))

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph"
            ) as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text="use manifest", session_id="default", data_dir=str(root / "default_data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("clarification_required", stdout)
            self.assertIn("stage_selection", stdout)
            build_graph.assert_not_called()

    def test_ask_with_explicit_run_config_runs_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            request = EntryRequest(session_id="s1", manifest=str(manifest), data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text="use manifest", session_id="s1", data_dir=str(root / "data"), run_config=str(config_path)),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn("clarification", stdout)
            self.assertIn("session_id: s1", stdout)

    def test_ask_materializes_source_path_and_emits_clarification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = root / "assembly.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(assembly)], data_dir=str(root / "data"))

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph"
            ) as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text=f"use {assembly}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("clarification_required", stdout)
            manifest = root / "data" / "manifests" / "s1" / "manifest.yaml"
            self.assertTrue(manifest.exists())
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("assembly_fasta_registered", text)
            build_graph.assert_not_called()

    def test_ask_raw_reads_emits_stage_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = root / "bht.Nanopore.pass.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            request = EntryRequest(
                session_id="s1",
                source_paths=[str(reads)],
                data_dir=str(root / "data"),
                platform="cycloneseq",
                asset_type_hint="raw_long_reads",
                genome_size_estimate=2_000_000,
            )

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph"
            ) as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text=f"帮我跑一下这个 cyclone文件：{reads}", data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("clarification_required", stdout)
            self.assertIn("stage_selection", stdout)
            self.assertIn("ask_plan_ref:", stdout)
            build_graph.assert_not_called()

    def test_ask_passes_explicit_run_config_and_checkpointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(json.dumps({"local_exec": ["read_qc"]}), encoding="utf-8")
            request = EntryRequest(session_id="s1", manifest=str(manifest), data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ) as build_graph, patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(
                        request_text="use manifest",
                        session_id="s1",
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("checkpoint_persistence: sqlite", stdout)
            self.assertIn(f"run_config_ref: {config_path.resolve(strict=False)}", stdout)
            build_graph.assert_called_once()
            self.assertEqual(build_graph.call_args.kwargs["backend"], "sqlite")
            self.assertEqual(graph.invocations[0][0]["stage_outputs"]["read_qc"]["execution_mode"], "local")

    def test_ask_rejects_unsupported_source_file_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes.txt"
            notes.write_text("not a biological input\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(notes)], data_dir=str(root / "data"))

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph"
            ) as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    self._ask_namespace(request_text=f"use {notes}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("unsupported source file type", stderr)
            build_graph.assert_not_called()

    def test_ask_lifecycle_failure_does_not_invoke_graph(self):
        with patch("app.entrypoints.intent.parse_request_with_llm", side_effect=ValueError("Lifecycle requests")), patch(
            "app.entrypoints.run.build_graph"
        ) as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_ask,
                self._ask_namespace(request_text="resume s1", session_id=None, data_dir=None),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Lifecycle requests", stderr)
        build_graph.assert_not_called()

    def test_ask_rejects_local_exec_without_run_config(self):
        ns = self._ask_namespace(request_text="run reads", session_id=None, data_dir=None)
        ns.local_exec = ["read_qc"]
        ns.run_config = None
        code, stdout, stderr = _capture_cli(_run_ask, ns)
        self.assertEqual(code, 1)
        self.assertIn("--local-exec is only valid with --run-config", stderr)

    def test_inspect_missing_session_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="missing", data_dir=str(Path(tmp) / "data")),
            )

        self.assertEqual(code, 1)
        self.assertIn("checkpoint_persistence: memory_only", stdout)
        self.assertIn("durable_resume_available: false", stdout)
        self.assertIn("session_not_found", stderr)

    def test_inspect_missing_session_json_returns_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="missing", data_dir=str(Path(tmp) / "data"), json=True),
            )

        self.assertEqual(code, 1)
        summary = json.loads(stdout)
        self.assertEqual(summary["error"], "session_not_found")
        self.assertEqual(summary["next_action"]["kind"], "inspect_unavailable")
        self.assertIn("session_not_found", stderr)

    def test_inspect_existing_session_prints_memory_only_limitation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            from app.control.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested("s1", "assembly_review", reason="needs_review")
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
            request_ref = _write_assembly_review_request(data_dir, "s1")
            from app.control.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="needs_review",
                review_request_artifact_ref=str(request_ref),
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
            self.assertIn("assembly_review_report.md", stdout)
            self.assertEqual(template["session_id"], "s1")
            self.assertEqual(template["stage"], "assembly_review")
            self.assertEqual(template["decision"], "accept")
            self.assertEqual(template["candidate_id"], "flye")
            self.assertEqual(template["final_assembly_ref"], "draft.fasta")
            self.assertEqual(template["decided_by"], "human")
            self.assertIn("assembly_review_report", template["metadata"]["template_guidance"]["report_refs"])

    def test_inspect_json_includes_review_next_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            request_ref = _write_assembly_review_request(data_dir, "s1")
            from app.control.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="needs_review",
                review_request_artifact_ref=str(request_ref),
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
            any(ref.endswith("assembly_review_report.md") for ref in summary["next_action"]["relevant_refs"])
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
                    main([
                        "resume",
                        "--session-id", "s1",
                        "--decision-file", str(root / "missing.json"),
                        "--data-dir", str(root / "data"),
                        "--checkpointer", "sqlite",
                    ])
                self.assertEqual(os.environ.get("GENE_AGENT_DOTENV_RESUME_PROBE"), "from-dotenv")
            finally:
                if old is None:
                    os.environ.pop("GENE_AGENT_DOTENV_RESUME_PROBE", None)
                else:
                    os.environ["GENE_AGENT_DOTENV_RESUME_PROBE"] = old


class RunConfigCLIIntegrationTests(unittest.TestCase):
    def _namespace(self, **overrides):
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
        return argparse.Namespace(**base)

    def test_run_config_seeds_local_exec_and_flye_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["read_qc", "assembly", "assembly_review"],
                        "read_qc": {
                            "length_filter": {
                                "method": "read_qc.length_filter.filtlong",
                                "approved": False,
                                "thresholds": {},
                            }
                        },
                        "assembly": {
                            "flye_parameters": {
                                "read_type": "nano-raw",
                                "estimated_genome_size": 2_000_000,
                                "threads": 4,
                            }
                        },
                        "assembly_review": {"quast": {"threads": 2}},
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
                    "method": "read_qc.length_filter.filtlong",
                    "approved": False,
                    "thresholds": {},
                },
            )
            self.assertEqual(
                stage_outputs["assembly"]["flye_parameters"],
                {
                    "read_type": "nano-raw",
                    "estimated_genome_size": 2_000_000,
                    "threads": 4,
                },
            )
            self.assertEqual(stage_outputs["assembly_review"]["quast_parameters"], {"threads": 2})

    def test_run_config_seeds_shared_parameters_and_flye_threads_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            config_path = root / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_exec": ["assembly"],
                        "shared_parameters": {
                            "read_type": "ont_raw",
                            "estimated_genome_size": 2_000_000,
                        },
                        "assembly": {"flye_parameters": {"threads": 4}},
                    }
                ),
                encoding="utf-8",
            )
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
                {"read_type": "ont_raw", "estimated_genome_size": 2_000_000},
            )
            self.assertEqual(state["stage_outputs"]["assembly"]["flye_parameters"], {"threads": 4})

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
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})

            def fake_initial_state(**kwargs):
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
                        manifest=str(manifest),
                        data_dir=str(root / "data"),
                        run_config=str(config_path),
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            repeat = graph.invocations[0][0]["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["execution_mode"], "local")
            self.assertEqual(repeat["primary_method"], "repeat_annotation.masking.repeatmasker")

    def test_missing_run_config_file_fails_before_graph_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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

    def test_parser_accepts_run_config_flag(self):
        args = _parse_args([
            "run",
            "--manifest",
            "m.yaml",
            "--run-config",
            "r.json",
        ])

        self.assertEqual(args.run_config, "r.json")

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
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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

    def test_cli_local_exec_assembly_without_run_config_fails_before_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
            with patch("app.entrypoints.run.build_graph") as build_graph, patch(
                "app.entrypoints.run.initial_state"
            ) as initial_state:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    self._namespace(
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
            "assembly.with_name(assembly.name + '.masked').write_text('>ctg1\\nacgtacgt\\n', encoding='utf-8')\n"
            "assembly.with_name(assembly.name + '.out.gff').write_text('##gff-version 3\\nctg1\\tRepeatMasker\\trepeat_region\\t1\\t8\\t.\\t+\\t.\\tID=repeat1\\n', encoding='utf-8')\n"
            "print('fake RepeatMasker ok')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def _write_existing_assembly_inputs(self, root: Path, *, session_id: str = "phase5-e2e") -> tuple[Path, Path, Path]:
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
            json.dumps({"local_exec": ["repeat_annotation"]}),
            encoding="utf-8",
        )
        return manifest, run_config, root / "out"

    def _run_args(self, *, session_id: str, manifest: Path, run_config: Path, data_dir: Path) -> argparse.Namespace:
        return argparse.Namespace(
            session_id=session_id,
            manifest=str(manifest),
            data_dir=str(data_dir),
            local_exec=[],
            run_config=str(run_config),
            checkpointer="memory",
        )

    def test_existing_assembly_repeatmasker_backbone_reaches_function_provenance_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-e2e"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(root, session_id=session_id)
            bin_dir = self._fake_repeatmasker_bin(root)
            args = self._run_args(session_id=session_id, manifest=manifest, run_config=run_config, data_dir=data_dir)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}), patch(
                "app.stages.repeat_annotation.nodes.resolve_method_env",
                return_value=(None, None),
            ):
                code, stdout, stderr = _capture_cli(_run_session, args)

            self.assertEqual(code, 1)
            self.assertIn("current_stage: function_annotation", stdout)
            self.assertIn("supervision_state: failed_terminal", stdout)
            self.assertIn("database_provenance", stderr)

            repeat_bundle = data_dir / "artifacts" / "repeat_annotation" / session_id / "repeat_annotation_bundle.json"
            gene_bundle = data_dir / "artifacts" / "gene_annotation" / session_id / "gene_annotation_bundle.json"
            function_bundle = data_dir / "artifacts" / "function_annotation" / session_id / "function_annotation_bundle.json"
            self.assertTrue(repeat_bundle.exists())
            self.assertTrue(gene_bundle.exists())
            self.assertFalse(function_bundle.exists())

            repeat = json.loads(repeat_bundle.read_text(encoding="utf-8"))
            gene = json.loads(gene_bundle.read_text(encoding="utf-8"))
            self.assertEqual(repeat["status"], "executed")
            self.assertEqual(repeat["repeat_library_source"], "provided")
            self.assertTrue(Path(repeat["softmasked_genome_ref"]).exists())
            self.assertTrue(Path(repeat["repeat_annotation_gff3_ref"]).exists())
            self.assertEqual(gene["genome_input_ref"], repeat["softmasked_genome_ref"])
            self.assertEqual(gene["genome_input_source"], "softmasked_genome")

            artifact_index = data_dir / "artifacts" / session_id / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifact_index.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("repeat_annotation.softmasked_genome", artifact_types)
            self.assertIn("repeat_annotation.gff3", artifact_types)
            self.assertIn("gene_annotation.bundle", artifact_types)
            self.assertIn("operator_report.failed_terminal", artifact_types)

            operator_report = data_dir / "artifacts" / "operator_report" / session_id / "failed_terminal_report.json"
            report = json.loads(operator_report.read_text(encoding="utf-8"))
            self.assertEqual(report["current_stage"], "function_annotation")
            self.assertEqual(report["next_action"]["kind"], "provenance_required")
            self.assertIn("database_provenance", report["last_error"])
            self.assertIn("input_proteins_ref", report["next_action"]["required_fields"])
            self.assertIn("database_source_ref", report["next_action"]["required_fields"])

    def test_repeatmasker_preflight_block_stops_before_command_and_gene_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-preflight-block"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(root, session_id=session_id)
            args = self._run_args(session_id=session_id, manifest=manifest, run_config=run_config, data_dir=data_dir)

            with patch(
                "app.execution.env_dispatch.preflight",
                return_value=PreflightResult(
                    status="missing_tool",
                    env_target="env_geno_exec",
                    missing_tools=["RepeatMasker"],
                    detail="missing tools in env_geno_exec: RepeatMasker",
                ),
            ) as preflight:
                code, stdout, stderr = _capture_cli(_run_session, args)

            self.assertEqual(code, 1)
            self.assertIn("current_stage: repeat_annotation", stdout)
            self.assertIn("supervision_state: failed_terminal", stdout)
            self.assertIn("RepeatMasker", stderr)
            preflight.assert_called_once()

            repeat_work = data_dir / "work" / "repeat_annotation" / session_id / "repeatmasker"
            gene_bundle = data_dir / "artifacts" / "gene_annotation" / session_id / "gene_annotation_bundle.json"
            self.assertFalse(repeat_work.exists())
            self.assertFalse(gene_bundle.exists())
            events_path = data_dir / "ledger" / session_id / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            blocks = [event for event in events if event["event_type"] == "env_preflight_blocked"]
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["method_name"], "repeat_annotation.masking.repeatmasker")
            self.assertEqual(blocks[0]["missing_tools"], ["RepeatMasker"])
            artifact_index = data_dir / "artifacts" / session_id / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifact_index.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("operator_report.failed_terminal", artifact_types)
            operator_report = data_dir / "artifacts" / "operator_report" / session_id / "failed_terminal_report.json"
            report = json.loads(operator_report.read_text(encoding="utf-8"))
            self.assertEqual(report["next_action"]["kind"], "missing_tool")
            self.assertEqual(report["next_action"]["missing_tools"], ["RepeatMasker"])
            self.assertEqual(report["next_action"]["env_target"], "env_geno_exec")

    def test_repeatmasker_failure_stops_before_gene_annotation_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-repeatmasker-fails"
            manifest, run_config, data_dir = self._write_existing_assembly_inputs(root, session_id=session_id)
            bin_dir = self._fake_repeatmasker_bin(root, fail=True)
            args = self._run_args(session_id=session_id, manifest=manifest, run_config=run_config, data_dir=data_dir)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}), patch(
                "app.stages.repeat_annotation.nodes.resolve_method_env",
                return_value=(None, None),
            ):
                code, stdout, stderr = _capture_cli(_run_session, args)

            self.assertEqual(code, 1)
            self.assertIn("current_stage: repeat_annotation", stdout)
            self.assertIn("supervision_state: failed_terminal", stdout)
            self.assertIn("fake RepeatMasker failed", stderr)
            repeat_bundle = data_dir / "artifacts" / "repeat_annotation" / session_id / "repeat_annotation_bundle.json"
            gene_bundle = data_dir / "artifacts" / "gene_annotation" / session_id / "gene_annotation_bundle.json"
            self.assertFalse(repeat_bundle.exists())
            self.assertFalse(gene_bundle.exists())
            artifact_index = data_dir / "artifacts" / session_id / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifact_index.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("command.stderr", artifact_types)


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
        assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        gene_models.write_text("##gff-version 3\nctg1\tprediction\tgene\t1\t8\t.\t+\t.\tID=gene1\n", encoding="utf-8")
        proteins.write_text(">prot1\nMACGT\n", encoding="utf-8")
        transcripts.write_text(">tx1\nACGTACGT\n", encoding="utf-8")
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
        run_config_payload = {"local_exec": []}
        if include_provenance:
            run_config_payload["function_annotation"] = {
                "database_provenance": [
                    {
                        "method_name": "function_annotation.candidate.interproscan",
                        "database_name": "InterPro",
                        "database_version": "99.0",
                        "snapshot_date": "2026-05-25",
                        "database_source_ref": "/db/interpro/99.0",
                        "descriptor_version": "interproscan.yaml",
                        "input_proteins_ref": str(proteins),
                    }
                ]
            }
        run_config.write_text(json.dumps(run_config_payload), encoding="utf-8")
        return manifest, run_config, root / "out"

    def _run_args(self, *, session_id: str, manifest: Path, run_config: Path, data_dir: Path) -> argparse.Namespace:
        return argparse.Namespace(
            session_id=session_id,
            manifest=str(manifest),
            data_dir=str(data_dir),
            local_exec=[],
            run_config=str(run_config),
            checkpointer="memory",
        )

    def test_existing_assembly_provided_annotation_reaches_function_provenance_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-provided-ann-e2e"
            manifest, run_config, data_dir = self._write_provided_annotation_inputs(root, session_id=session_id)
            args = self._run_args(session_id=session_id, manifest=manifest, run_config=run_config, data_dir=data_dir)

            code, stdout, stderr = _capture_cli(_run_session, args)

            self.assertEqual(code, 1)
            self.assertIn("current_stage: function_annotation", stdout)
            self.assertIn("supervision_state: failed_terminal", stdout)
            self.assertIn("database_provenance", stderr)

            gene_bundle_path = data_dir / "artifacts" / "gene_annotation" / session_id / "gene_annotation_bundle.json"
            function_bundle_path = data_dir / "artifacts" / "function_annotation" / session_id / "function_annotation_bundle.json"
            self.assertTrue(gene_bundle_path.exists())
            self.assertFalse(function_bundle_path.exists())

            gene = json.loads(gene_bundle_path.read_text(encoding="utf-8"))
            self.assertEqual(gene["status"], "adopted_existing_annotation")
            self.assertEqual(gene["evidence_profile"], "provided_annotation")
            self.assertEqual(gene["candidate_methods"], [])

            # refs must point to the actual manifest-provided files, not work-path placeholders
            gene_models_file = root / "gene_models.gff3"
            proteins_file = root / "predicted_proteins.fasta"
            transcripts_file = root / "transcripts.fa"
            self.assertEqual(gene["genes_gff3_ref"], str(gene_models_file))
            self.assertEqual(gene["predicted_proteins_ref"], str(proteins_file))
            self.assertEqual(gene["transcripts_fasta_ref"], str(transcripts_file))

            artifact_index = data_dir / "artifacts" / session_id / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifact_index.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.provided_gene_models", artifact_types)
            self.assertIn("gene_annotation.provided_predicted_proteins", artifact_types)
            self.assertIn("gene_annotation.provided_transcripts_fasta", artifact_types)
            self.assertIn("gene_annotation.bundle", artifact_types)
            self.assertIn("operator_report.failed_terminal", artifact_types)

            operator_report = data_dir / "artifacts" / "operator_report" / session_id / "failed_terminal_report.json"
            report = json.loads(operator_report.read_text(encoding="utf-8"))
            self.assertEqual(report["current_stage"], "function_annotation")
            self.assertEqual(report["next_action"]["kind"], "provenance_required")
            self.assertIn("input_proteins_ref", report["next_action"]["required_fields"])
            self.assertIn("database_source_ref", report["next_action"]["required_fields"])

    def test_existing_assembly_provided_annotation_with_provenance_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "phase5-function-provenance-e2e"
            manifest, run_config, data_dir = self._write_provided_annotation_inputs(
                root,
                session_id=session_id,
                include_provenance=True,
            )
            args = self._run_args(session_id=session_id, manifest=manifest, run_config=run_config, data_dir=data_dir)

            code, stdout, stderr = _capture_cli(_run_session, args)

            self.assertEqual(code, 0, stderr)
            self.assertIn("current_stage: done", stdout)
            self.assertIn("supervision_state: completed", stdout)

            proteins_file = root / "predicted_proteins.fasta"
            function_bundle_path = data_dir / "artifacts" / "function_annotation" / session_id / "function_annotation_bundle.json"
            final_report_path = data_dir / "artifacts" / "final_report" / session_id / "review_decision_report.json"
            self.assertTrue(function_bundle_path.exists())
            self.assertTrue(final_report_path.exists())

            bundle = json.loads(function_bundle_path.read_text(encoding="utf-8"))
            self.assertEqual(bundle["proteins_fasta_ref"], str(proteins_file))
            self.assertEqual(bundle["database_provenance"][0]["input_proteins_ref"], str(proteins_file))
            self.assertEqual(bundle["database_provenance"][0]["database_source_ref"], "/db/interpro/99.0")

            artifact_index = data_dir / "artifacts" / session_id / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifact_index.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("function_annotation.plan", artifact_types)
            self.assertIn("function_annotation.bundle", artifact_types)
            self.assertIn("final_report.review_decisions", artifact_types)

            events_path = data_dir / "ledger" / session_id / "events.jsonl"
            ledger_types = [
                json.loads(line)["artifact"]["artifact_type"]
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["event_type"] == "artifact_registered"
            ]
            self.assertIn("function_annotation.plan", ledger_types)
            self.assertIn("function_annotation.bundle", ledger_types)
            self.assertIn("final_report.review_decisions", ledger_types)


class ResumeCLITests(unittest.TestCase):
    def _write_decision(self, path: Path, **overrides) -> Path:
        payload = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "accept",
            "candidate_id": "flye",
            "final_assembly_ref": "draft.fasta",
            "notes": ["test"],
        }
        payload.update(overrides)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_parser_accepts_resume_subcommand(self):
        args = _parse_args(
            [
                "resume",
                "--session-id", "s1",
                "--decision-file", "decision.json",
                "--data-dir", "out",
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
            decision = self._write_decision(Path(tmp) / "decision.json", session_id="other")
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


def _capture_cli(func, *args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
