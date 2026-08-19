"""Focused contract tests for the deterministic ``gene-agent run`` command."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.entrypoints.run as run_entry
from app.control.lifecycle import TerminalFinalizationResult
from app.control.projections import (
    ProjectionBoundary,
    progress_projection_input,
)
from app.control.resume_transaction import ResumeTransactionError
from app.graph.builder import build_graph
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state as build_initial_state
from app.storage.frozen_inputs import FrozenExecutionLease
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.busco_fixture import ensure_busco_lineage


def _capture_call(function, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        result = function(*args, **kwargs)
    return result, stdout.getvalue(), stderr.getvalue()


def _busco_config(lineage_path: str) -> dict[str, object]:
    return {
        "lineage_path": lineage_path,
        "lineage_name": "embryophyta_odb10",
        "lineage_version": "odb10",
        "lineage_source": "test_fixture",
        "threads": 4,
    }


def _leave_blocked(_graph, _config, result, *_args, **_kwargs):
    return result


def _active_lease(paths: SessionPaths) -> FrozenExecutionLease:
    return FrozenExecutionLease(
        session_id=paths.session_id,
        run_dir=paths.run_dir,
        active=True,
    )


def _finalized_terminal(*args, **_kwargs) -> TerminalFinalizationResult:
    return TerminalFinalizationResult(state=args[2], terminal_event={})


def _completed_state(state: dict, *, config: dict) -> dict:
    del config
    return {
        **state,
        "current_stage": "done",
        "supervision_state": "completed",
        "workflow_step_index": len(state["workflow_steps"]),
        "entered_workflow_step_index": len(state["workflow_steps"]) - 1,
        "route_trace": [],
    }


def _blocked_state(state: dict, *, config: dict) -> dict:
    del config
    index = state["workflow_step_index"]
    return {
        **state,
        "current_stage": state["workflow_steps"][index]["stage"],
        "supervision_state": "blocked_human",
        "entered_workflow_step_index": index,
        "route_trace": ["assembly_stage"],
        "active_interrupt": None,
    }


def _prepared_inputs(
    *,
    session_id: str = "manifest-session",
    requested_stages: list[str] | None = None,
    local_stages: tuple[str, ...] = (),
):
    stages = list(requested_stages or [])
    payload: dict[str, object] = {
        "requested_stages": [
            {
                "stage": stage,
                "allow_local_execution": stage in local_stages,
            }
            for stage in stages
        ],
    }
    if "assembly_review" in stages:
        payload["assembly_review"] = {
            "busco": _busco_config("/data/busco/embryophyta_odb10")
        }
    return SimpleNamespace(
        manifest=SimpleNamespace(session_id=session_id),
        run_config=RunConfig.model_validate(payload),
    )


class RunParserTests(unittest.TestCase):
    def test_run_parser_exposes_exact_approved_namespace(self):
        args = run_entry._parse_args(
            [
                "run",
                "--manifest",
                "manifest.yaml",
                "--run-config",
                "run.json",
                "--data-dir",
                "data",
            ]
        )

        self.assertEqual(
            vars(args),
            {
                "command": "run",
                "manifest": "manifest.yaml",
                "run_config": "run.json",
                "data_dir": "data",
            },
        )

    def test_help_advertises_run_and_only_approved_run_options(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as top_exit:
            with contextlib.redirect_stdout(stdout):
                run_entry._parse_args(["--help"])
        self.assertEqual(top_exit.exception.code, 0)
        self.assertIn("run", stdout.getvalue())

        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as run_exit:
            with contextlib.redirect_stdout(stdout):
                run_entry._parse_args(["run", "--help"])
        self.assertEqual(run_exit.exception.code, 0)
        help_text = stdout.getvalue()
        for option in (
            "--manifest",
            "--run-config",
            "--data-dir",
        ):
            self.assertIn(option, help_text)
        for forbidden in (
            "--session-id",
            "--checkpointer",
            "--local-exec",
            "--non-interactive",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_run_requires_all_three_paths_and_argparse_returns_two(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(stderr):
                run_entry._parse_args(["run", "--manifest", "manifest.yaml"])

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--run-config", stderr.getvalue())
        self.assertIn("--data-dir", stderr.getvalue())

    def test_run_rejects_legacy_and_unapproved_arguments(self):
        base = [
            "run",
            "--manifest",
            "manifest.yaml",
            "--run-config",
            "run.json",
            "--data-dir",
            "data",
        ]
        for extra in (
            ["--session-id", "cli-owned"],
            ["--checkpointer", "memory"],
            ["--local-exec", "assembly"],
            ["--non-interactive"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_entry._parse_args([*base, *extra])
                self.assertEqual(caught.exception.code, 2)

        for command in ("launch", "ask"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_entry._parse_args([command])
                self.assertEqual(caught.exception.code, 2)


class InspectTextTests(unittest.TestCase):
    def test_print_inspect_text_includes_current_progress_and_execution(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "running",
            "checkpoint_persistence": "sqlite",
            "durable_resume_available": True,
            "current_workflow_step_id": "step-002-read_qc",
            "current_stage": "read_qc",
            "artifact_count": 0,
            "decisions_by_origin": {},
            "workflow_progress": {
                "plan_source": "frozen_run_config",
                "total_steps": 3,
                "completed_steps": 1,
                "skipped_steps": 1,
                "failed_steps": 0,
                "blocked_steps": 0,
                "unfinished_steps": 1,
                "projection_authority": "degraded_stage_terminal_projection",
            },
            "stage_terminal_integrity": [
                {
                    "kind": "conflicting_stage_terminal",
                    "workflow_step_id": "step-002-read_qc",
                    "stage": "read_qc",
                }
            ],
            "execution": {
                "frozen_inputs_status": "verified",
                "durable_progress": "checkpoint",
                "execution_lock_held": True,
            },
            "next_action": {"kind": "unknown", "summary": "No action."},
        }

        _result, stdout, stderr = _capture_call(
            run_entry._print_inspect_text, summary, {}
        )

        self.assertEqual(stderr, "")
        self.assertIn("current_workflow_step_id: step-002-read_qc", stdout)
        self.assertIn("current_stage: read_qc", stdout)
        self.assertIn(
            "workflow_progress: finished=2/3 completed=1 skipped=1 failed=0 "
            "blocked=0 unfinished=1 source=frozen_run_config",
            stdout,
        )
        self.assertIn(
            "execution: frozen_inputs=verified durable_progress=checkpoint "
            "lock_held=true",
            stdout,
        )
        self.assertIn("WARNING: workflow_progress is a degraded projection", stdout)
        self.assertIn("WARNING: stage_terminal_integrity:", stdout)

    def test_print_inspect_text_does_not_warn_for_authoritative_projection(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "running",
            "checkpoint_persistence": "sqlite",
            "durable_resume_available": True,
            "current_workflow_step_id": "step-001-read_qc",
            "current_stage": "read_qc",
            "artifact_count": 0,
            "decisions_by_origin": {},
            "workflow_progress": {
                "plan_source": "frozen_run_config",
                "total_steps": 2,
                "completed_steps": 1,
                "skipped_steps": 0,
                "failed_steps": 0,
                "blocked_steps": 0,
                "unfinished_steps": 1,
                "projection_authority": "authoritative",
            },
            "stage_terminal_integrity": [],
            "execution": {
                "frozen_inputs_status": "verified",
                "durable_progress": "checkpoint",
                "execution_lock_held": False,
            },
            "next_action": {"kind": "unknown", "summary": "No action."},
        }

        _result, stdout, stderr = _capture_call(
            run_entry._print_inspect_text, summary, {}
        )

        self.assertEqual(stderr, "")
        self.assertNotIn("degraded projection", stdout)


class AttachedProgressTests(unittest.TestCase):
    def test_renderer_uses_frozen_occurrence_cursor_without_persisting_projection(self):
        state = {
            "workflow_steps": [
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-read_qc",
                    "stage": "read_qc",
                    "allow_local_execution": False,
                },
            ],
            "workflow_step_index": 1,
            "current_stage": "read_qc",
            "supervision_state": "running_stage",
            "skip_reasons": {"step-000-inventory": "no registered inputs"},
        }

        _result, stdout, stderr = _capture_call(
            run_entry._render_attached_progress,
            progress_projection_input(
                state,
                stage_terminals={
                    ("step-000-inventory", "inventory"): {"event_type": "stage_skipped"}
                },
            ),
        )

        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "progress: finished=1/2 completed=0 skipped=1 "
            "current=step-001-read_qc stage=read_qc state=running\n",
        )

    def test_early_failure_finished_count_ignores_terminal_cursor_position(self):
        stages = [
            "inventory",
            "read_qc",
            "genome_survey",
            "assembly",
            "assembly_dedup",
            "assembly_review",
        ]
        state = {
            "session_id": "s1",
            "workflow_steps": [
                {
                    "step_id": f"step-{index:03d}-{stage}",
                    "stage": stage,
                    "allow_local_execution": False,
                    **(
                        {
                            "review_target": {
                                "resource_type": "assembly",
                                "producer_step_id": "step-004-assembly_dedup",
                            }
                        }
                        if stage == "assembly_review"
                        else {}
                    ),
                }
                for index, stage in enumerate(stages)
            ],
            "workflow_step_index": 0,
            "entered_workflow_step_index": 0,
            "current_stage": "inventory",
            "supervision_state": "failed_terminal",
            "workflow_step_outcome": {
                "workflow_step_id": "step-000-inventory",
                "stage": "inventory",
                "outcome": "failed",
                "reason": "early failure",
            },
        }

        _result, stdout, stderr = _capture_call(
            run_entry._render_attached_progress,
            progress_projection_input(
                state,
                stage_terminals={
                    ("step-000-inventory", "inventory"): {"event_type": "stage_failed"}
                },
            ),
        )

        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "progress: finished=0/6 completed=0 skipped=0 "
            "current=- stage=- state=failed\n",
        )

    def test_degraded_attached_progress_never_renders_done_silently(self):
        state = {
            "session_id": "s1",
            "workflow_steps": [
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-read_qc",
                    "stage": "read_qc",
                    "allow_local_execution": False,
                },
            ],
            "workflow_step_index": 1,
            "entered_workflow_step_index": 0,
            "current_stage": "read_qc",
            "supervision_state": "running_stage",
        }

        _result, stdout, stderr = _capture_call(
            run_entry._render_attached_progress,
            progress_projection_input(
                state,
                stage_terminals={
                    ("step-000-inventory", "inventory"): {
                        "event_type": "stage_completed"
                    }
                },
                terminal_integrity=({"kind": "conflicting_stage_terminal"},),
            ),
        )

        self.assertEqual(
            stdout,
            "progress: finished=1/2 completed=1 skipped=0 "
            "current=- stage=- state=running\n",
        )
        self.assertEqual(
            stderr,
            "warning: attached workflow progress is degraded because stage "
            "terminal history is inconsistent.\n",
        )


class RunHandlerTests(unittest.TestCase):
    def test_manifest_identity_owns_paths_and_sqlite_dispatch(self):
        prepared = _prepared_inputs(requested_stages=["assembly"])
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="source/manifest.yaml",
                run_config="source/run.json",
                data_dir=tmp,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ) as prepare,
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch(
                    "app.entrypoints.run.execute_frozen_session", return_value=0
                ) as execute,
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.execution.run_readiness.preflight_run_readiness"
                ) as readiness,
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            manifest_draft="source/manifest.yaml",
            run_config_draft="source/run.json",
        )
        frozen_paths = freeze.call_args.args[0]
        self.assertEqual(frozen_paths.session_id, "manifest-session")
        self.assertEqual(frozen_paths.data_root, Path(tmp).resolve())
        self.assertEqual(
            freeze.call_args.kwargs,
            {
                "prepared_inputs": prepared,
                "selected_methods": {},
            },
        )
        execute.assert_called_once_with(
            "manifest-session",
            Path(tmp).resolve(),
            checkpointer="sqlite",
            interactive_gate=True,
        )
        readiness.assert_called_once_with(
            prepared.run_config,
            effective_methods={},
        )

    def test_structured_run_reuses_canonical_closure_for_readiness_and_freeze(self):
        prepared = _prepared_inputs(
            requested_stages=["read_qc"],
            local_stages=("read_qc",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="source/manifest.yaml",
                run_config="source/run.json",
                data_dir=tmp,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch(
                    "app.entrypoints.run.execute_frozen_session",
                    return_value=0,
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.execution.run_readiness.preflight_run_readiness"
                ) as readiness,
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        effective = {
            "read_qc": ("read_qc.long_read_stats.seqkit_stats",),
        }
        readiness.assert_called_once_with(
            prepared.run_config,
            effective_methods=effective,
        )
        self.assertEqual(freeze.call_args.kwargs["selected_methods"], effective)

    def test_non_tty_run_fails_before_freeze_or_execute(self):
        prepared = _prepared_inputs(requested_stages=["read_qc"])
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_dir),
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=False,
                ),
                patch(
                    "app.entrypoints.run.execute_frozen_session", return_value=0
                ) as execute,
            ):
                code, stdout, stderr = _capture_call(run_entry._run, args)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("real stdin/stdout TTY", stderr)
        self.assertFalse(data_dir.exists())
        freeze.assert_not_called()
        execute.assert_not_called()

    def test_null_requested_stages_fails_before_freeze_or_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = root / "assembly.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: explicit-null-plan\n"
                "assets:\n"
                "  - asset_type: assembly_fasta_registered\n"
                f"    paths: [{assembly}]\n",
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": None,
                    }
                ),
                encoding="utf-8",
            )
            data_dir = root / "data"
            args = argparse.Namespace(
                manifest=str(manifest),
                run_config=str(run_config),
                data_dir=str(data_dir),
                non_interactive=False,
            )

            with (
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, stdout, stderr = _capture_call(run_entry._run, args)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("requested_stages", stderr)
        self.assertIn("valid list", stderr)
        self.assertFalse(data_dir.exists())
        freeze.assert_not_called()
        execute.assert_not_called()

    def test_registered_assembly_does_not_restrict_the_requested_plan(self):
        for stage in ("read_qc", "genome_survey", "assembly", "assembly_dedup"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                prepared = _prepared_inputs(requested_stages=[stage])
                prepared.manifest.assets = [
                    SimpleNamespace(asset_type="assembly_fasta_registered")
                ]
                args = argparse.Namespace(
                    manifest="manifest.yaml",
                    run_config="run.json",
                    data_dir=tmp,
                    non_interactive=False,
                )
                with (
                    patch(
                        "app.storage.frozen_inputs.prepare_session_inputs",
                        return_value=prepared,
                    ),
                    patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                    patch(
                        "app.entrypoints.run._foreground_review_available",
                        return_value=True,
                    ),
                    patch(
                        "app.entrypoints.run.execute_frozen_session", return_value=0
                    ) as execute,
                    patch("app.execution.run_readiness.preflight_run_readiness"),
                ):
                    code, stdout, stderr = _capture_call(run_entry._run, args)

                self.assertEqual(code, 0)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")
                freeze.assert_called_once()
                self.assertNotIn("selected_stages", freeze.call_args.kwargs)
                execute.assert_called_once()

    def test_preflight_failures_are_normalized(self):
        args = argparse.Namespace(
            manifest="manifest.yaml",
            run_config="run.json",
            data_dir="data",
            non_interactive=False,
        )
        with (
            patch(
                "app.storage.frozen_inputs.prepare_session_inputs",
                side_effect=ValueError("malformed run config"),
            ),
            patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
            patch("app.entrypoints.run.execute_frozen_session") as execute,
        ):
            code, stdout, stderr = _capture_call(run_entry._run, args)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "error: run preflight failed: malformed run config\n",
        )
        freeze.assert_not_called()
        execute.assert_not_called()

    def test_readiness_failure_does_not_freeze_or_create_session(self):
        prepared = _prepared_inputs(requested_stages=["read_qc"])
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_dir),
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.execution.run_readiness.preflight_run_readiness",
                    side_effect=ValueError("seqkit missing"),
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, _stdout, stderr = _capture_call(run_entry._run, args)

            self.assertEqual(code, 1)
            self.assertIn("seqkit missing", stderr)
            self.assertFalse(data_dir.exists())
            freeze.assert_not_called()
            execute.assert_not_called()

    def test_inventory_only_missing_dependency_config_creates_no_session(self):
        prepared = _prepared_inputs(requested_stages=[])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_dir),
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": str(root / "missing-envs.yaml")},
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, _stdout, stderr = _capture_call(run_entry._run, args)

            self.assertEqual(code, 1)
            self.assertIn("dependency configuration is not ready", stderr)
            self.assertFalse(data_dir.exists())
            freeze.assert_not_called()
            execute.assert_not_called()

    def test_inventory_only_malformed_dependency_yaml_returns_preflight_error(self):
        prepared = _prepared_inputs(requested_stages=[])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            envs = root / "envs.yaml"
            envs.write_text("default: [unterminated\n", encoding="utf-8")
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_dir),
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs)}),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, _stdout, stderr = _capture_call(run_entry._run, args)

            self.assertEqual(code, 1)
            self.assertIn("dependency configuration is not ready", stderr)
            self.assertIn("env config YAML is invalid", stderr)
            self.assertFalse(data_dir.exists())
            freeze.assert_not_called()
            execute.assert_not_called()

    def test_noncanonical_manifest_session_id_fails_before_freeze(self):
        prepared = _prepared_inputs(
            session_id="./manifest-session",
            requested_stages=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_root),
                non_interactive=False,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, stdout, stderr = _capture_call(run_entry._run, args)

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("session_id must be one path component", stderr)
            self.assertFalse(data_root.exists())
            freeze.assert_not_called()
            execute.assert_not_called()

    def test_main_dispatches_run_handler(self):
        argv = [
            "run",
            "--manifest",
            "manifest.yaml",
            "--run-config",
            "run.json",
            "--data-dir",
            "data",
        ]
        with (
            patch("app.entrypoints.run._load_dotenv_without_override"),
            patch("app.entrypoints.run._run", return_value=7) as run_handler,
        ):
            code = run_entry.main(argv)

        self.assertEqual(code, 7)
        run_handler.assert_called_once()
        self.assertEqual(run_handler.call_args.args[0].command, "run")


class PublicRunSQLiteIntegrationTests(unittest.TestCase):
    def test_registered_assembly_with_supporting_asset_starts_at_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "public-run-existing-with-support"
            assembly = root / "existing_assembly.fa"
            proteins = root / "reference_proteins.fa"
            assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            lineage = ensure_busco_lineage(root)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                f"session_id: {session_id}\n"
                "assets:\n"
                "  - asset_type: assembly_fasta_registered\n"
                f"    paths: [{assembly}]\n"
                "  - asset_type: reference_proteins\n"
                f"    paths: [{proteins}]\n",
                encoding="utf-8",
            )
            run_config = root / "run.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {"stage": "assembly_review", "allow_local_execution": False}
                        ],
                        "assembly_review": {"busco": _busco_config(str(lineage))},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.entrypoints.run._interactive_gate_loop",
                    side_effect=_leave_blocked,
                ),
            ):
                code, stdout, stderr = _capture_call(
                    run_entry.main,
                    [
                        "run",
                        "--manifest",
                        str(manifest),
                        "--run-config",
                        str(run_config),
                        "--data-dir",
                        str(root / "data"),
                    ],
                )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("current_stage: assembly_review", stdout)
            self.assertIn("supervision_state: blocked_human", stdout)
            events = Ledger(
                SessionPaths(root / "data", session_id).run_dir
            ).read_events(session_id)
            registered = [
                event
                for event in events
                if event["event_type"] == "registered_assembly_candidate_bound"
            ]
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]["workflow_step_id"], "step-000-inventory")

    def test_interactive_run_persists_and_reopens_blocked_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "public-run-sqlite-gate"
            assembly = root / "existing_assembly.fa"
            assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            lineage = ensure_busco_lineage(root)

            manifest = root / "manifest.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        f"session_id: {session_id}",
                        "sample_id: sqlite-gate-sample",
                        "assets:",
                        "  - asset_type: assembly_fasta_registered",
                        f"    paths: [{assembly}]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {"stage": "assembly_review", "allow_local_execution": False}
                        ],
                        "assembly_review": {"busco": _busco_config(str(lineage))},
                    }
                ),
                encoding="utf-8",
            )
            data_dir = root / "data"

            with (
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.entrypoints.run._interactive_gate_loop",
                    side_effect=_leave_blocked,
                ),
            ):
                code, stdout, stderr = _capture_call(
                    run_entry.main,
                    [
                        "run",
                        "--manifest",
                        str(manifest),
                        "--run-config",
                        str(run_config),
                        "--data-dir",
                        str(data_dir),
                    ],
                )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("supervision_state: blocked_human", stdout)
            self.assertIn("checkpoint_persistence: sqlite", stdout)

            paths = SessionPaths(data_dir, session_id)
            self.assertTrue(paths.checkpoint_db_path.is_file())
            with sqlite3.connect(paths.checkpoint_db_path) as connection:
                checkpoint_count = connection.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                    (session_id,),
                ).fetchone()[0]
            self.assertGreater(checkpoint_count, 0)

            reopened_graph = build_graph(
                backend="sqlite",
                data_dir=str(paths.run_dir),
            )
            try:
                snapshot = reopened_graph.get_state(
                    {"configurable": {"thread_id": session_id}}
                )
                self.assertEqual(
                    snapshot.values["supervision_state"],
                    "blocked_human",
                )
                self.assertEqual(snapshot.values["current_stage"], "assembly_review")
                self.assertIsNotNone(snapshot.values["active_interrupt"])
            finally:
                saver = reopened_graph.checkpointer
                if hasattr(saver, "conn"):
                    saver.conn.close()


class FrozenExecutionBoundaryTests(unittest.TestCase):
    def test_validation_progress_and_lock_delegate_verified_bundle(self):
        bundle = SimpleNamespace()

        @contextlib.contextmanager
        def fake_lock(paths):
            yield _active_lease(paths)

        with tempfile.TemporaryDirectory() as tmp:
            expected_paths = SessionPaths(tmp, "frozen-session")
            with (
                patch(
                    "app.storage.frozen_inputs.frozen_execution_lock",
                    side_effect=fake_lock,
                ) as lock,
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=bundle,
                ) as validate,
                patch(
                    "app.storage.frozen_inputs.frozen_execution_progress",
                    return_value="checkpoint",
                ) as progress,
                patch(
                    "app.entrypoints.run._execute_verified_frozen_session",
                    return_value=0,
                ) as execute,
            ):
                code = run_entry.execute_frozen_session(
                    "frozen-session",
                    tmp,
                    checkpointer="memory",
                    interactive_gate=False,
                )

        self.assertEqual(code, 0)
        lock.assert_called_once_with(expected_paths)
        validate.assert_called_once_with(expected_paths)
        progress.assert_called_once_with(expected_paths)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args, (bundle,))
        self.assertEqual(
            execute.call_args.kwargs,
            {
                "checkpointer": "memory",
                "resume_checkpoint": True,
                "interactive_gate": False,
                "lease": execute.call_args.kwargs["lease"],
            },
        )
        self.assertIsInstance(
            execute.call_args.kwargs["lease"],
            FrozenExecutionLease,
        )

    def test_partial_execution_is_normalized_before_helper(self):
        @contextlib.contextmanager
        def fake_lock(_paths):
            yield

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "app.storage.frozen_inputs.frozen_execution_lock",
                    side_effect=fake_lock,
                ),
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "app.storage.frozen_inputs.frozen_execution_progress",
                    return_value="partial",
                ),
                patch(
                    "app.entrypoints.run._execute_verified_frozen_session"
                ) as execute,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry.execute_frozen_session,
                    "frozen-session",
                    tmp,
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error:", stderr)
        self.assertIn("partial execution evidence", stderr)
        execute.assert_not_called()


class FrozenHelperTests(unittest.TestCase):
    def test_frozen_run_initializes_occurrence_aware_workflow_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig.model_validate(
                    {
                        "requested_stages": [
                            {"stage": "t2t", "allow_local_execution": False},
                            {
                                "stage": "assembly_review",
                                "allow_local_execution": False,
                            },
                            {"stage": "t2t", "allow_local_execution": True},
                            {
                                "stage": "assembly_review",
                                "allow_local_execution": False,
                            },
                        ],
                        "t2t": {"mode": "manual_review"},
                        "assembly_review": {
                            "busco": _busco_config("/data/busco/embryophyta_odb10")
                        },
                        "review_policy": {
                            "max_blocked_seconds": 300,
                            "stages": {
                                "t2t": {"max_blocked_seconds": 0},
                            },
                        },
                    }
                ),
            )
            graph = Mock()
            graph.invoke.side_effect = _completed_state
            with (
                patch("app.entrypoints.run.Ledger"),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ),
                patch("app.entrypoints.run.project_workflow_state"),
            ):
                code, _stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        invoked_state = graph.invoke.call_args.args[0]
        self.assertEqual(
            invoked_state["workflow_steps"],
            [
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                    "selected_method_names": [],
                },
                {
                    "step_id": "step-001-t2t",
                    "stage": "t2t",
                    "allow_local_execution": False,
                    "selected_method_names": ["t2t.alignment.minimap2"],
                },
                {
                    "step_id": "step-002-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "selected_method_names": [
                        "assembly_review.eval.quast",
                        "assembly_review.eval.busco",
                    ],
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-001-t2t",
                    },
                },
                {
                    "step_id": "step-003-t2t",
                    "stage": "t2t",
                    "allow_local_execution": True,
                    "selected_method_names": ["t2t.alignment.minimap2"],
                },
                {
                    "step_id": "step-004-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "selected_method_names": [
                        "assembly_review.eval.quast",
                        "assembly_review.eval.busco",
                    ],
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-003-t2t",
                    },
                },
            ],
        )
        self.assertEqual(invoked_state["stage_plan_ref"], "control/run_config.json")
        self.assertEqual(
            invoked_state["stage_seeds"],
            {
                "assembly_review": {
                    "busco_parameters": {
                        **_busco_config("/data/busco/embryophyta_odb10"),
                        "mode": "genome",
                    },
                    "review_policy": {"max_blocked_seconds": 300},
                },
                "t2t": {
                    "mode": "manual_review",
                    "read_inputs": [],
                    "review_policy": {"max_blocked_seconds": 0},
                },
            },
        )
        self.assertEqual(invoked_state["stage_outputs"], {})

    def test_noninteractive_block_uses_frozen_authority_and_prints_guidance(self):
        with tempfile.TemporaryDirectory(prefix="gene agent ") as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            graph = Mock()
            graph.invoke.side_effect = _blocked_state

            with (
                patch("app.entrypoints.run.Ledger") as ledger,
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=build_initial_state,
                ) as initial,
                patch("app.entrypoints.run.build_graph", return_value=graph) as build,
                patch("app.entrypoints.run._interactive_gate_loop") as gate_loop,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        initial.assert_called_once_with(
            session_id="frozen-session",
            input_manifest_ref="control/input_manifest.yaml",
            data_dir=str(paths.run_dir),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                    "selected_method_names": [],
                }
            ],
            stage_plan_ref="control/run_config.json",
            stage_seeds={},
        )
        ledger.assert_called_once_with(str(paths.run_dir))
        ledger.return_value.create_session.assert_called_once_with(
            "frozen-session", "control/input_manifest.yaml"
        )
        build.assert_called_once()
        self.assertEqual(build.call_args.kwargs["backend"], "sqlite")
        self.assertEqual(build.call_args.kwargs["data_dir"], str(paths.run_dir))
        self.assertIs(build.call_args.kwargs["verified_inputs"], bundle)
        self.assertIsNone(build.call_args.kwargs["on_controller_transition"])
        self.assertIsInstance(
            build.call_args.kwargs["projection_boundary"], ProjectionBoundary
        )
        graph.invoke.assert_called_once_with(
            graph.invoke.call_args.args[0],
            config={"configurable": {"thread_id": "frozen-session"}},
        )
        gate_loop.assert_not_called()
        self.assertIn("supervision_state: blocked_human", stdout)
        self.assertIn("inspect_command: gene-agent inspect", stdout)
        self.assertIn("review_command: gene-agent review", stdout)
        self.assertIn("resume_command: gene-agent resume", stdout)
        self.assertIn("--session-id frozen-session", stdout)
        self.assertIn("--data-dir", stdout)
        self.assertIn(f"'{paths.data_root}'", stdout)
        self.assertIn(
            f"run_config_ref: {paths.run_config_path.resolve(strict=False)}", stdout
        )

    def test_empty_requested_stages_produces_inventory_only_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            graph = Mock()
            graph.invoke.side_effect = _completed_state
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=build_initial_state,
                ) as initial,
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ),
                patch("app.entrypoints.run.project_workflow_state"),
            ):
                code, _stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            initial.call_args.kwargs["workflow_steps"],
            [
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                    "selected_method_names": [],
                }
            ],
        )
        self.assertEqual(initial.call_args.kwargs["stage_seeds"], {})

    def test_interactive_resume_validation_failure_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            graph = Mock()
            graph.invoke.side_effect = _blocked_state
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=build_initial_state,
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph) as build,
                patch(
                    "app.entrypoints.run._interactive_gate_loop",
                    side_effect=ResumeTransactionError(
                        "validation",
                        "stale review decision",
                    ),
                ) as gate_loop,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=True,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "error: resume validation failed: stale review decision\n",
        )
        self.assertTrue(gate_loop.call_args.kwargs["lease"].active)
        self.assertIsInstance(
            gate_loop.call_args.kwargs["projection_boundary"], ProjectionBoundary
        )
        self.assertIs(
            build.call_args.kwargs["on_controller_transition"],
            run_entry._render_attached_progress,
        )

    def test_existing_blocked_checkpoint_does_not_restore_foreground_countdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            blocked_state = build_initial_state(
                session_id="frozen-session",
                input_manifest_ref="control/input_manifest.yaml",
                data_dir=str(paths.run_dir),
            )
            blocked_state.update(
                {
                    "supervision_state": "blocked_human",
                    "entered_workflow_step_index": 0,
                }
            )
            graph = Mock()
            graph.get_state.return_value = SimpleNamespace(values=blocked_state)
            with (
                patch("app.entrypoints.run.Ledger") as ledger,
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=build_initial_state,
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run._interactive_gate_loop") as gate_loop,
            ):
                ledger.return_value.read_canonical_terminal.return_value = None
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=True,
                    interactive_gate=True,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        graph.invoke.assert_not_called()
        gate_loop.assert_not_called()
        self.assertIn("countdown is not restored across processes", stdout)
        self.assertIn("review_command: gene-agent review", stdout)

    def test_existing_terminal_checkpoint_uses_canonical_stage_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            terminal_state = build_initial_state(
                session_id="frozen-session",
                input_manifest_ref="control/input_manifest.yaml",
                data_dir=str(paths.run_dir),
            )
            terminal_state.update(
                {
                    "supervision_state": "completed",
                    "active_interrupt": None,
                }
            )
            canonical = {
                "session_id": "frozen-session",
                "stage": "assembly",
                "summary": {"outcome": "completed"},
            }
            graph = Mock()
            graph.get_state.return_value = SimpleNamespace(values=terminal_state)
            reconcile_target = run_entry.reconcile_canonical_terminal_checkpoint
            with (
                patch("app.entrypoints.run.Ledger") as ledger,
                patch(
                    "app.entrypoints.run.initial_state",
                    side_effect=build_initial_state,
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.reconcile_canonical_terminal_checkpoint",
                    wraps=reconcile_target,
                ) as reconcile,
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=run_entry.TerminalFinalizationError(
                        "canonical Ledger terminal stage conflicts with the checkpoint"
                    ),
                ),
            ):
                ledger.return_value.read_canonical_terminal.return_value = {
                    "canonical": canonical,
                    "terminal_integrity": None,
                }
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=True,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("stage conflicts with the checkpoint", stderr)
        reconcile.assert_called_once_with(
            terminal_state,
            canonical,
            session_id="frozen-session",
        )
        graph.invoke.assert_not_called()

    def test_graph_exception_persists_normalized_terminal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(
                    requested_stages=[
                        {"stage": "assembly", "allow_local_execution": False},
                        {"stage": "assembly_review", "allow_local_execution": False},
                    ],
                    assembly_review={
                        "busco": _busco_config("/data/busco/embryophyta_odb10")
                    },
                ),
            )
            graph = Mock()
            graph.invoke.side_effect = RuntimeError("stage exploded")
            graph.get_state.return_value = SimpleNamespace(
                values={"current_stage": "assembly", "route_trace": ["inventory"]}
            )
            with (
                patch("app.entrypoints.run.Ledger") as ledger,
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ) as finalize,
            ):
                ledger.return_value.read_canonical_terminal.return_value = None
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                    lease=_active_lease(paths),
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "error: workflow invoke failed: RuntimeError: stage exploded", stderr
        )
        failure = finalize.call_args.args[2]
        self.assertEqual(failure["session_id"], "frozen-session")
        self.assertEqual(failure["data_dir"], str(paths.run_dir))
        self.assertEqual(failure["input_manifest_ref"], "control/input_manifest.yaml")
        self.assertEqual(failure["supervision_state"], "failed_terminal")
        self.assertEqual(failure["last_error"], "RuntimeError: stage exploded")
        self.assertEqual(failure["current_stage"], "assembly")
        self.assertEqual(
            finalize.call_args.kwargs,
            {"normalize_failure": True},
        )


if __name__ == "__main__":
    unittest.main()
