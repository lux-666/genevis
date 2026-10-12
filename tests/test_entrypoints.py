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
from unittest.mock import patch

from app.control.projections import ProjectionBoundary
from app.control.resume_transaction import (
    ResumeTransactionError,
    ResumeTransactionResult,
)
from app.control.terminal import TerminalFinalizationError, TerminalFinalizationResult
from app.entrypoints.run import (
    _execute_verified_frozen_session,
    _load_dotenv_without_override,
    _parse_args,
    _resolve_checkpointer_backend,
    _run_inspect,
    _run_resume,
    _run_storage_audit,
    generate_session_id,
)
from app.entrypoints.tui import _tui_main, run_tui
from app.execution.dependency_snapshot import (
    FrozenDependencyResolver,
    capture_dependency_snapshot,
)
from app.execution.env_resolver import EnvResolver
from app.review.base import ReviewPayload
from app.review.persistence import (
    PersistedReviewRequest,
    persist_review_request,
)
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
    AssemblyCandidateBundle,
)
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.storage.artifacts import publish_artifact_record
from app.storage.frozen_inputs import FrozenExecutionLease, freeze_session_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout
from tests.busco_fixture import (
    assembly_review_config,
    busco_parameters,
    ensure_busco_lineage,
    ensure_test_host_envs,
)
from tests.data_v2 import v2_run_dir


class _FakeGraph:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.invocations: list[tuple[dict, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return {
            **state,
            **self.result,
            "workflow_step_index": len(state["workflow_steps"]),
            "entered_workflow_step_index": len(state["workflow_steps"]) - 1,
        }


def _active_lease(paths: SessionPaths) -> FrozenExecutionLease:
    return FrozenExecutionLease(
        session_id=paths.session_id,
        run_dir=paths.run_dir,
        active=True,
    )


def _frozen_database_resolver(root: Path, lineage: Path) -> FrozenDependencyResolver:
    envs = ensure_test_host_envs(root, lineage_path=lineage)
    active = EnvResolver(envs)
    snapshot = capture_dependency_snapshot(
        config_path=envs, resolver=active, probe_environments=False
    )
    return FrozenDependencyResolver(snapshot)


def _finalized_terminal(*args, **_kwargs) -> TerminalFinalizationResult:
    return TerminalFinalizationResult(state=args[2], terminal_event={})


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
        path_ref=(
            "artifacts/steps/step-001-assembly_review/assembly_review/"
            f"{candidate_id}{suffix}"
        ),
        sha256="a" * 64,
        size_bytes=10,
        artifact_type=artifact_type,
    ).model_dump(mode="json")


def _publish_executed_assembly_candidate(
    paths: SessionPaths,
    *,
    workflow_step_id: str,
    stage: str,
):
    work_dir = paths.workflow_step_work_dir(workflow_step_id, stage)
    work_dir.mkdir(parents=True, exist_ok=True)
    method_name = "assembly_review.contract"
    candidate_id = "flye"

    assembly_source = work_dir / "final.fasta"
    assembly_source.write_text(">contig1\nACGT\n", encoding="utf-8")
    assembly = publish_artifact_record(
        paths.run_dir,
        paths.session_id,
        ArtifactMeta.from_file(
            assembly_source,
            stage=stage,
            method_name=method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        ),
    )
    planned = AssemblyCandidateBundle(
        session_id=paths.session_id,
        candidate_id=candidate_id,
        method_name=method_name,
        status="planned",
        input_type="clean_long_reads",
        input_member_ids={"primary": ["reads-1"]},
        input_member_platforms={"primary": ["ont"]},
        parameter_attempt_id=0,
        expected_outputs=["primary_assembly"],
    )
    planned_source = work_dir / "candidate.planned.json"
    planned_source.write_text(planned.model_dump_json(), encoding="utf-8")
    planned_artifact = publish_artifact_record(
        paths.run_dir,
        paths.session_id,
        ArtifactMeta.from_file(
            planned_source,
            stage=stage,
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
    )
    executed = planned.model_copy(
        update={
            "status": "executed",
            "plan_ref": planned_artifact.ref,
            "primary_output": assembly.ref,
        }
    )
    executed_source = work_dir / "candidate.executed.json"
    executed_source.write_text(executed.model_dump_json(), encoding="utf-8")
    executed_artifact = publish_artifact_record(
        paths.run_dir,
        paths.session_id,
        ArtifactMeta.from_file(
            executed_source,
            stage=stage,
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
    )
    return executed_artifact, assembly


def _write_assembly_review_request(
    data_root: Path,
    session_id: str,
) -> tuple[Path, PersistedReviewRequest]:
    run_dir = v2_run_dir(data_root, session_id)
    paths = SessionPaths.from_run_dir(run_dir)
    reads = run_dir / "fixture_assembly.fa"
    reads.write_text(">ctg\nACGT\n", encoding="utf-8")
    lineage = ensure_busco_lineage(run_dir)
    envs = run_dir / "envs.yaml"
    _frozen_database_resolver(run_dir, lineage)
    freeze_session_inputs(
        paths,
        manifest_draft={
            "session_id": session_id,
            "user_metadata": {"target_species": "Fixture species"},
            "assets": [
                {
                    "asset_type": "assembly_fasta_registered",
                    "paths": [str(reads.resolve())],
                    "format": "fasta",
                }
            ],
        },
        run_config_draft={
            "requested_stages": [
                {"stage": "assembly_review", "allow_local_execution": False}
            ],
            "assembly_review": assembly_review_config(lineage),
        },
        selected_methods={},
        config_path=envs,
        probe_environments=False,
    )
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
        report_refs={
            "assembly_review_report": "reports/assembly_review.md",
            "review_packet": (
                "artifacts/steps/step-001-assembly_review/assembly_review/"
                "assembly_review_packet.json"
            ),
        },
        candidate_comparison_context={
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _artifact_ref_json("flye")},
        },
    )
    state = {
        "session_id": session_id,
        "data_dir": str(run_dir),
        "input_manifest_ref": paths.to_ref(paths.manifest_path),
        "current_stage": "assembly_review",
        "workflow_steps": [
            {
                "selected_method_names": [],
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "selected_method_names": [],
                "step_id": "step-001-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": False,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-000-inventory",
                },
            },
        ],
        "workflow_step_index": 1,
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
    ledger = Ledger(run_dir)
    ledger.create_session(session_id, paths.to_ref(paths.manifest_path))
    ledger.record_stage_completed(
        session_id,
        "inventory",
        workflow_step_id="step-000-inventory",
    )
    return run_dir, persisted


class EntrypointTests(unittest.TestCase):
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
        self.assertIn("Solea ovata", stdout.getvalue())

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
            ]
        )

        self.assertEqual(args.command, "storage-audit")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.format, "json")
        self.assertFalse(hasattr(args, "dry_run_manifest"))
        self.assertFalse(hasattr(args, "apply"))

    def test_storage_audit_cli_reports_usage_without_cleanup_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            report = paths.report_path("session_summary.json")
            report.write_text("{}\n", encoding="utf-8")
            work = paths.workflow_step_work_path(
                "step-001-assembly", "assembly", "resume-required.txt"
            )
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
                ]
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = _run_storage_audit(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["layers"]["reports"]["file_count"], 1)
        self.assertEqual(payload["layers"]["work"]["file_count"], 1)
        self.assertNotIn("reclaimable", payload)
        self.assertNotIn("cleanup_manifest", payload)

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
            self.assertNotIn("template_guidance", template["metadata"])
            self.assertEqual(template["decided_by"], "human")

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

    def test_inspect_completed_session_exposes_verified_final_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            ensure_run_layout(paths)
            workflow_step_id = "step-001-assembly_review"
            candidate, assembly = _publish_executed_assembly_candidate(
                paths,
                workflow_step_id=workflow_step_id,
                stage="assembly_review",
            )
            ledger = Ledger(run_dir)
            ledger.create_session("s1", paths.to_ref(paths.manifest_path))
            ledger.record_resource_delivered(
                "s1",
                stage="assembly_review",
                workflow_step_id=workflow_step_id,
                resource_type="assembly",
                delivery_mode="reused",
                candidate_bundle_ref=candidate.ref,
                primary_output_ref=assembly.ref,
            )
            ledger.record_stage_completed(
                "s1",
                "assembly_review",
                workflow_step_id=workflow_step_id,
            )
            ledger.record_session_terminal(
                "s1", outcome="completed", stage="assembly_review"
            )

            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_root), json=True),
            )
            summary = json.loads(stdout)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIsNotNone(summary["final_assembly"], summary)
            self.assertEqual(
                summary["final_assembly"]["primary_output_path"], str(assembly.path)
            )
            self.assertEqual(
                summary["final_assembly"]["primary_output_ref"],
                assembly.ref.model_dump(mode="json"),
            )

            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_root)),
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(f"final_assembly: {assembly.path}", stdout)

            assembly.path.write_text(">tampered\nAAAA\n", encoding="utf-8")
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_root), json=True),
            )
            summary = json.loads(stdout)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIsNone(summary["final_assembly"])
            self.assertIn(
                "integrity validation", summary["final_assembly_integrity_error"]
            )

    def test_inspect_rejects_delivery_without_completed_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            ensure_run_layout(paths)
            workflow_step_id = "step-001-assembly_review"
            candidate, assembly = _publish_executed_assembly_candidate(
                paths,
                workflow_step_id=workflow_step_id,
                stage="assembly_review",
            )
            ledger = Ledger(run_dir)
            ledger.create_session("s1", paths.to_ref(paths.manifest_path))
            ledger.record_resource_delivered(
                "s1",
                stage="assembly_review",
                workflow_step_id=workflow_step_id,
                resource_type="assembly",
                delivery_mode="reused",
                candidate_bundle_ref=candidate.ref,
                primary_output_ref=assembly.ref,
            )
            ledger.record_session_terminal(
                "s1", outcome="completed", stage="assembly_review"
            )

            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_root), json=True),
            )
            summary = json.loads(stdout)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIsNone(summary["final_assembly"])
            self.assertIn(
                "not canonically completed",
                summary["final_assembly_integrity_error"],
            )

    def test_dotenv_loader_uses_gene_agent_local_env_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "other"
            other.mkdir()
            (other / ".env").write_text(
                "GENE_AGENT_LLM_MODEL=from-cwd\n", encoding="utf-8"
            )
            (root / ".env").write_text(
                "GENE_AGENT_LLM_API_KEY=sk-from-dotenv\nGENE_AGENT_LLM_MODEL=from-dotenv\n",
                encoding="utf-8",
            )
            old_key = os.environ.get("GENE_AGENT_LLM_API_KEY")
            old_model = os.environ.get("GENE_AGENT_LLM_MODEL")
            try:
                os.environ["GENE_AGENT_LLM_API_KEY"] = "sk-existing"
                os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                with (
                    patch.dict(
                        os.environ,
                        {"GENE_AGENT_ENVS_CONFIG": str(root / "config" / "envs.yaml")},
                    ),
                    patch("os.getcwd", return_value=str(other)),
                ):
                    _load_dotenv_without_override()
                    self.assertEqual(
                        os.environ["GENE_AGENT_LLM_API_KEY"], "sk-existing"
                    )
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
                with patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": str(root / "config" / "envs.yaml")},
                ):
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
                        os.environ.get("GENE_AGENT_DOTENV_RESUME_PROBE"),
                        "from-dotenv",
                    )
            finally:
                if old is None:
                    os.environ.pop("GENE_AGENT_DOTENV_RESUME_PROBE", None)
                else:
                    os.environ["GENE_AGENT_DOTENV_RESUME_PROBE"] = old

    def test_bootstrap_explicit_config_selects_only_its_project_dotenv(self):
        from app.entrypoints.run import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            with (
                patch(
                    "app.entrypoints.run._load_dotenv_without_override"
                ) as load_dotenv,
                patch("app.entrypoints.run._run_bootstrap", return_value=0),
            ):
                code = main(["bootstrap", "--config", str(config_path)])

            self.assertEqual(code, 0)
            load_dotenv.assert_called_once_with([root.resolve() / ".env"])


class FrozenRunConfigSeedingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._resolver_tmp = tempfile.TemporaryDirectory()
        root = Path(self._resolver_tmp.name)
        resolver = _frozen_database_resolver(root, ensure_busco_lineage(root))
        self.addCleanup(self._resolver_tmp.cleanup)
        self._resolver_patch = patch(
            "app.execution.dependency_snapshot.load_frozen_dependency_resolver",
            return_value=resolver,
        )
        self._resolver_patch.start()
        self.addCleanup(self._resolver_patch.stop)

    def _execute_config(self, run_config: RunConfig) -> tuple[int, dict, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lineage = ensure_busco_lineage(root)
            resolver = _frozen_database_resolver(root, lineage)
            payload = run_config.model_dump(mode="json", exclude_none=True)
            review = payload.get("assembly_review")
            if isinstance(review, dict) and isinstance(review.get("busco"), dict):
                requested = review["busco"]
                review["busco"] = {
                    "mode": requested.get("mode", "genome"),
                    "threads": requested.get("threads", 4),
                }
            execution_config = RunConfig.model_validate(payload)
            paths = SessionPaths(tmp, "s1")
            bundle = SimpleNamespace(paths=paths, run_config=execution_config)
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fake_initial_state(**kwargs):
                return initial_state(**kwargs)

            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.execution.dependency_snapshot.load_frozen_dependency_resolver",
                    return_value=resolver,
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=fake_initial_state,
                ),
                patch(
                    "app.control.terminal.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ),
                patch("app.entrypoints.run.project_workflow_state"),
            ):
                code, stdout, stderr = _capture_cli(
                    _execute_verified_frozen_session,
                    bundle,
                    checkpointer="memory",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        state = graph.invocations[0][0] if graph.invocations else {}
        return code, state, stdout, stderr

    def test_frozen_executor_seeds_verified_run_config_fields(self):
        run_config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "read_qc", "allow_local_execution": True},
                    {"stage": "genome_survey", "allow_local_execution": False},
                    {"stage": "assembly", "allow_local_execution": True},
                    {"stage": "assembly_review", "allow_local_execution": True},
                ],
                "shared_parameters": {"read_type": "ont_raw"},
                "read_qc": {
                    "length_filter": {
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
                "assembly_review": {
                    "quast": {"threads": 2},
                    "busco": busco_parameters("/data/busco/embryophyta_odb10"),
                },
            }
        )

        code, state, _stdout, stderr = self._execute_config(run_config)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(state["shared_parameters"], {"read_type": "ont_raw"})
        stage_seeds = state["stage_seeds"]
        for stage in (
            "read_qc",
            "assembly",
            "assembly_review",
        ):
            self.assertNotIn("execution_mode", stage_seeds[stage])
        self.assertEqual(
            stage_seeds["read_qc"]["length_filter_request"]["method"],
            "skip",
        )
        self.assertEqual(
            stage_seeds["genome_survey"]["survey_parameters"]["kmer_size"],
            21,
        )
        # Assembly method and parameters are stage-time recommendations, not
        # RunConfig input.  The seed may carry review policy, but never a
        # method selection or method-specific parameter block.
        self.assertNotIn("assembly_selection", stage_seeds["assembly"])
        self.assertNotIn("flye_parameters", stage_seeds["assembly"])
        self.assertEqual(
            stage_seeds["assembly"]["review_policy"], {"max_blocked_seconds": 300}
        )
        self.assertEqual(
            stage_seeds["assembly_review"]["review_policy"],
            {"max_blocked_seconds": 300},
        )
        self.assertEqual(
            stage_seeds["assembly_review"]["quast_parameters"],
            {"threads": 2},
        )

    def test_frozen_executor_rejects_legacy_assembly_method_and_parameters(self):
        with self.assertRaises(ValueError):
            RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "assembly", "allow_local_execution": True},
                        {
                            "stage": "assembly_review",
                            "allow_local_execution": False,
                        },
                    ],
                    "shared_parameters": {"platform": "ont"},
                    "assembly": {
                        "assembly_method": "hifiasm_ont",
                        "hifiasm_ont_parameters": {"threads": 4},
                    },
                    "assembly_review": assembly_review_config(
                        "/data/busco/embryophyta_odb10"
                    ),
                }
            )

    def test_terminal_finalization_failure_flushes_projection_warning_once(self):
        run_config = RunConfig(requested_stages=[])
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            bundle = SimpleNamespace(paths=paths, run_config=run_config)
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                }
            )

            def fail_projection() -> None:
                raise RuntimeError("renderer unavailable")

            def build_with_failed_projection(**kwargs):
                kwargs["projection_boundary"].run("progress", fail_projection)
                return graph

            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.build_graph",
                    side_effect=build_with_failed_projection,
                ),
                patch(
                    "app.control.terminal.finalize_authoritative_terminal",
                    side_effect=TerminalFinalizationError("checkpoint mismatch"),
                ),
            ):
                code, stdout, stderr = _capture_cli(
                    _execute_verified_frozen_session,
                    bundle,
                    checkpointer="memory",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        warning = (
            "warning: progress projection failed: RuntimeError: renderer unavailable"
        )
        self.assertEqual(code, 1)
        self.assertIn("supervision_state: completed", stdout)
        self.assertEqual(stderr.count(warning), 1)
        self.assertIn(
            "error: terminal finalization failed: checkpoint mismatch",
            stderr,
        )

    def test_interactive_outer_failure_flushes_projection_warning_once(self):
        run_config = RunConfig(requested_stages=[])
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            bundle = SimpleNamespace(paths=paths, run_config=run_config)

            class _BlockedGraph:
                def invoke(self, state, config):
                    return {
                        **state,
                        "supervision_state": "blocked_human",
                        "active_interrupt": {
                            "stage": "inventory",
                            "workflow_step_id": "step-000-inventory",
                        },
                        "stage_outputs": {"inventory": {"status": "blocked_human"}},
                    }

            graph = _BlockedGraph()

            def fail_projection() -> None:
                raise RuntimeError("operator report unavailable")

            def fail_interactive(*_args, **kwargs):
                kwargs["projection_boundary"].run("operator_report", fail_projection)
                raise ResumeTransactionError("checkpoint", "checkpoint update failed")

            with (
                patch("app.entrypoints.run.Ledger"),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run._interactive_gate_loop",
                    side_effect=fail_interactive,
                ),
            ):
                code, stdout, stderr = _capture_cli(
                    _execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=True,
                    lease=_active_lease(paths),
                )

        warning = (
            "warning: operator_report projection failed: RuntimeError: "
            "operator report unavailable"
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count(warning), 1)
        self.assertIn(
            "error: resume checkpoint failed: checkpoint update failed",
            stderr,
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

    def test_parser_accepts_committed_intent_recovery_without_decision_file(self):
        args = _parse_args(["resume", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "resume")
        self.assertEqual(args.session_id, "s1")
        self.assertIsNone(args.decision_file)
        self.assertEqual(args.data_dir, "out")

    def test_resume_rejects_removed_max_blocked_seconds_override(self):
        with self.assertRaises(SystemExit):
            _parse_args(
                [
                    "resume",
                    "--session-id",
                    "s1",
                    "--decision-file",
                    "decision.json",
                    "--max-blocked-seconds",
                    "60",
                ]
            )

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

    def test_resume_threads_one_verified_bundle_to_graph_and_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            paths.checkpoint_db_path.write_text("", encoding="utf-8")
            decision = self._write_decision(Path(tmp) / "decision.json")
            verified_inputs = object()
            graph = object()
            transaction_result = ResumeTransactionResult(
                state={
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                    "active_interrupt": None,
                },
                decision_artifact_path=None,
            )

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=verified_inputs,
                ) as validate,
                patch(
                    "app.entrypoints.run.build_graph",
                    return_value=graph,
                ) as build,
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    return_value=transaction_result,
                ) as execute,
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id="s1",
                        decision_file=str(decision),
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                    ),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        validate.assert_called_once_with(paths)
        self.assertIs(build.call_args.kwargs["verified_inputs"], verified_inputs)
        self.assertIsInstance(
            build.call_args.kwargs["projection_boundary"], ProjectionBoundary
        )
        self.assertIsInstance(execute.call_args.kwargs["lease"], FrozenExecutionLease)
        self.assertIsInstance(
            execute.call_args.kwargs["projection_boundary"], ProjectionBoundary
        )

    def test_resume_without_decision_file_recovers_committed_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            paths.checkpoint_db_path.write_text("", encoding="utf-8")
            transaction_result = ResumeTransactionResult(
                state={
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                    "active_interrupt": None,
                },
                decision_artifact_path=None,
            )

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=object(),
                ),
                patch("app.entrypoints.run.build_graph", return_value=object()),
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    return_value=transaction_result,
                ) as execute,
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id="s1",
                        decision_file=None,
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                    ),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIsNone(execute.call_args.kwargs["request"])
        self.assertEqual(execute.call_args.kwargs["source"], "cli_recovery")
        self.assertEqual(execute.call_args.kwargs["event_context"], {})

    def test_resume_failure_still_prints_isolated_projection_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            paths.checkpoint_db_path.write_text("", encoding="utf-8")

            def fail_projection() -> None:
                raise RuntimeError("status projection unavailable")

            def fail_transaction(**kwargs) -> None:
                kwargs["projection_boundary"].run(
                    "review_status",
                    fail_projection,
                )
                raise ResumeTransactionError("checkpoint", "checkpoint update failed")

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=object(),
                ),
                patch("app.entrypoints.run.build_graph", return_value=object()),
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    side_effect=fail_transaction,
                ),
            ):
                code, stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id="s1",
                        decision_file=None,
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                    ),
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "warning: review_status projection failed: RuntimeError: "
            "status projection unavailable",
            stderr,
        )
        self.assertIn(
            "error: resume checkpoint failed: checkpoint update failed",
            stderr,
        )

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
            state = initial_state(
                session_id=session_id,
                input_manifest_ref=paths.to_ref(paths.manifest_path),
                data_dir=str(run_dir),
                workflow_steps=[
                    {
                        "selected_method_names": [],
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "selected_method_names": [],
                        "step_id": "step-001-assembly_review",
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-000-inventory",
                        },
                    },
                ],
            )
            state.update(
                {
                    "supervision_state": "blocked_human",
                    "current_stage": "assembly_review",
                    "workflow_step_index": 1,
                    "entered_workflow_step_index": 1,
                    "active_interrupt": persisted.compact_interrupt(
                        stage="assembly_review",
                        gate_id="assembly_review",
                        reason="needs_review",
                    ),
                    "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                }
            )
            graph = _FakeResumeGraph(state)

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=SimpleNamespace(
                        run_config=RunConfig(requested_stages=[])
                    ),
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    return_value=ResumeTransactionResult(
                        state={
                            "current_stage": "done",
                            "supervision_state": "completed",
                            "route_trace": [],
                        },
                        decision_artifact_path=None,
                    ),
                ) as execute,
            ):
                code, _stdout, stderr = _capture_cli(
                    _run_resume,
                    argparse.Namespace(
                        session_id=session_id,
                        decision_file=str(decision),
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(execute.call_args.kwargs["data_dir"], run_dir)
            self.assertIsInstance(
                execute.call_args.kwargs["lease"], FrozenExecutionLease
            )


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
