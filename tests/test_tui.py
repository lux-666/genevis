"""Integration tests for the TUI-owned intake, freeze, and resume path."""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.entrypoints.tui import (
    _apply_llm_plan_draft,
    _freeze_and_execute,
    _generate_plan_draft_with_configured_llm,
    _plan_draft_context,
    _resume_tui_session,
    _start_tui_session,
    _tui_main,
    run_tui,
)
from app.entrypoints.tui_intake import (
    apply_asset_answer,
    create_tui_session_draft,
    deterministic_workflow_intent,
    load_tui_session_draft,
    materialize_manifest_draft,
    next_intake_clarification,
    save_tui_session_draft,
)
from app.integrity import sha256_file
from app.schemas.ask_plan import AskPlanState, PlanStageOccurrence
from app.schemas.run_config import RunConfig
from app.schemas.shared_parameters import SharedParameters
from app.storage.frozen_inputs import (
    FrozenExecutionActiveError,
    freeze_session_inputs,
    frozen_execution_lock,
)
from app.storage.paths import SessionPaths
from app.workflow import MANUAL_ONLY_STAGES, recommended_workflow_template
from tests.busco_fixture import ensure_busco_lineage, ensure_test_host_envs


class TuiWorkflowTests(unittest.TestCase):
    def _reads(self, root: Path, name: str = "reads.fastq") -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        return path

    def test_llm_plan_draft_accepts_inventory_only_json_list(self):
        plan = AskPlanState(
            session_id="s1",
            data_dir="data",
            entry_request={},
            manifest_ref="test.yaml",
        )

        result = _apply_llm_plan_draft(plan, {"selected_stage_occurrences": []})

        self.assertEqual(result.selected_stage_occurrences, [])

    def test_llm_plan_draft_requires_a_json_list_for_stage_occurrences(self):
        plan = AskPlanState(
            session_id="s1",
            data_dir="data",
            entry_request={},
            manifest_ref="test.yaml",
        )
        for payload in ({}, {"selected_stage_occurrences": None}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "JSON list"):
                    _apply_llm_plan_draft(plan, payload)

    def test_llm_plan_draft_rejects_method_selection(self):
        plan = AskPlanState(
            session_id="s1",
            data_dir="data",
            entry_request={},
            manifest_ref="test.yaml",
        )

        with self.assertRaisesRegex(ValueError, "must not choose methods"):
            _apply_llm_plan_draft(
                plan,
                {
                    "selected_stage_occurrences": [
                        {"stage": "assembly", "allow_local_execution": True}
                    ],
                    "selected_methods": {"assembly": "assembly.candidate.flye"},
                },
            )

    def test_llm_plan_prompt_consumes_canonical_workflow_context(self):
        plan = AskPlanState(
            session_id="s1",
            data_dir="data",
            request_text="inventory only",
            entry_request={},
            manifest_ref="missing.yaml",
        )
        context = _plan_draft_context(plan)
        captured: dict[str, str] = {}

        def invoke(system: str, user: str):
            captured["system"] = system
            captured["user"] = user
            return {"selected_stage_occurrences": []}

        with patch("app.entrypoints.tui._invoke_json_model", side_effect=invoke):
            result = _generate_plan_draft_with_configured_llm(plan)

        self.assertEqual(result.selected_stage_occurrences, [])
        self.assertEqual(
            context["canonical_t2t_workflow"],
            recommended_workflow_template("t2t"),
        )
        self.assertEqual(
            context["manual_only_stages"],
            sorted(MANUAL_ONLY_STAGES),
        )
        self.assertIn("canonical_t2t_workflow", captured["system"])
        self.assertIn('"canonical_t2t_workflow"', captured["user"])
        self.assertNotIn(
            "includes read_qc, genome_survey, assembly",
            captured["system"],
        )

    def test_initial_goal_can_confirm_ont_and_hic_assets_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            ont = self._reads(root, "ont.fastq")
            r1 = self._reads(root, "hic-r1.fastq")
            r2 = self._reads(root, "hic-r2.fastq")
            captured = {}

            def continue_draft(draft):
                captured["draft"] = draft
                return 0

            request = f"T2T 组装；ONT: {ont}; Hi-C R1: {r1}; R2: {r2}"

            with (
                patch(
                    "app.entrypoints.run.generate_session_id",
                    return_value="s1",
                ),
                patch(
                    "app.entrypoints.tui._parse_goal_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._parse_assets_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._continue_tui_draft",
                    side_effect=continue_draft,
                    return_value=0,
                ),
            ):
                code = _start_tui_session(str(data_dir), request)

            self.assertEqual(code, 0)
            self.assertEqual(
                captured["draft"].intake.ont_reads.paths,
                [str(ont.resolve())],
            )
            self.assertEqual(
                captured["draft"].intake.hic_reads.pairs[0].r1,
                str(r1.resolve()),
            )
            # Assets are confirmed; T2T without species still needs the batch question.
            clarification = next_intake_clarification(captured["draft"])
            self.assertIsNotNone(clarification)
            self.assertEqual(clarification.kind, "t2t_batch")
            paths = SessionPaths(data_dir, "s1")
            self.assertTrue(paths.manifest_draft_path.is_file())

    def test_initial_goal_does_not_assign_unlabelled_path_to_ont(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            captured = {}

            def continue_draft(draft):
                captured["draft"] = draft
                return 0

            with (
                patch("app.entrypoints.run.generate_session_id", return_value="s1"),
                patch(
                    "app.entrypoints.tui._parse_goal_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._parse_assets_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._continue_tui_draft",
                    side_effect=continue_draft,
                ),
            ):
                code = _start_tui_session(str(data_dir), f"T2T assembly {reads}")

            self.assertEqual(code, 0)
            self.assertIsNone(captured["draft"].intake.ont_reads)

    def test_top_level_resume_command_selects_a_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )
            save_tui_session_draft(draft)

            with (
                patch("builtins.input", side_effect=["/resume", "1", ""]),
                patch(
                    "app.entrypoints.tui._resume_tui_session",
                    return_value=0,
                ) as resume,
            ):
                code = _tui_main(str(data_dir))

            self.assertEqual(code, 0)
            resume.assert_called_once_with(str(data_dir), "s1")

    def test_resume_continues_same_unfrozen_intake_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )
            save_tui_session_draft(draft)

            def collect_plan(updated):
                return updated, object()

            with (
                patch("builtins.input", side_effect=["", str(reads)]),
                patch(
                    "app.entrypoints.tui._parse_assets_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._collect_run_plan",
                    side_effect=collect_plan,
                ),
                patch(
                    "app.entrypoints.tui._freeze_and_execute",
                    return_value=0,
                ),
            ):
                code = run_tui(str(data_dir), resume_session="s1")

            self.assertEqual(code, 0)
            resumed = load_tui_session_draft(data_dir, "s1")
            self.assertEqual(resumed.intake.ont_reads.paths, [str(reads.resolve())])
            self.assertEqual(resumed.lifecycle, "planning")

    def test_frozen_blocked_session_routes_to_review_without_input_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            envs = self._write_envs(root)
            parent = self._frozen_parent(data_dir, reads, envs)
            paths = SessionPaths(data_dir, parent.session_id)
            before = {
                key: sha256_file(path)
                for key, path in {
                    "marker": paths.frozen_inputs_path,
                    "manifest": paths.input_manifest_path,
                    "run_config": paths.run_config_path,
                    "dependencies": paths.dependency_manifest_path,
                }.items()
            }

            captured: dict[str, object] = {}

            def review_while_locked(*_args, lease):
                lease.require_active(paths)
                captured["lease"] = lease
                with self.assertRaises(FrozenExecutionActiveError):
                    with frozen_execution_lock(paths):
                        pass
                return 1

            with (
                patch(
                    "app.entrypoints.tui._durable_supervision_state",
                    return_value="blocked_human",
                ),
                patch(
                    "app.entrypoints.review_interactive.run_interactive_review",
                    side_effect=review_while_locked,
                ) as review,
            ):
                code = _resume_tui_session(str(data_dir), parent.session_id)

            self.assertEqual(code, 1)
            review.assert_called_once_with(
                parent.session_id,
                str(paths.run_dir),
                lease=captured["lease"],
            )
            self.assertEqual(
                load_tui_session_draft(data_dir, parent.session_id).lifecycle,
                "blocked_review",
            )
            self.assertEqual(before["marker"], sha256_file(paths.frozen_inputs_path))
            self.assertEqual(before["manifest"], sha256_file(paths.input_manifest_path))
            self.assertEqual(before["run_config"], sha256_file(paths.run_config_path))
            self.assertEqual(
                before["dependencies"], sha256_file(paths.dependency_manifest_path)
            )

    def test_resume_starts_unlocked_frozen_session_without_execution_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            parent = self._frozen_parent(
                data_dir,
                self._reads(root),
                self._write_envs(root),
            ).model_copy(update={"lifecycle": "frozen"})
            save_tui_session_draft(parent)

            with patch(
                "app.entrypoints.run.execute_frozen_session",
                return_value=0,
            ) as execute:
                code = _resume_tui_session(str(data_dir), parent.session_id)

            self.assertEqual(code, 0)
            execute.assert_called_once_with(
                parent.session_id,
                parent.data_dir,
                checkpointer="sqlite",
                interactive_gate=True,
            )
            self.assertEqual(
                load_tui_session_draft(data_dir, parent.session_id).lifecycle,
                "terminal",
            )

    def test_resume_continues_unlocked_checkpoint_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            parent = self._frozen_parent(
                data_dir,
                self._reads(root),
                self._write_envs(root),
            ).model_copy(update={"lifecycle": "running"})
            save_tui_session_draft(parent)
            paths = SessionPaths(data_dir, parent.session_id)
            connection = sqlite3.connect(paths.checkpoint_db_path)
            try:
                connection.execute(
                    "CREATE TABLE checkpoints ("
                    "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, "
                    "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, "
                    "type TEXT, checkpoint BLOB, metadata BLOB, "
                    "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
                )
                connection.execute(
                    "INSERT INTO checkpoints VALUES (?, '', 'cp1', NULL, "
                    "'msgpack', ?, ?)",
                    (parent.session_id, b"checkpoint", b"metadata"),
                )
                connection.commit()
            finally:
                connection.close()

            with patch(
                "app.entrypoints.run.execute_frozen_session",
                return_value=0,
            ) as execute:
                code = _resume_tui_session(str(data_dir), parent.session_id)

            self.assertEqual(code, 0)
            execute.assert_called_once()

    def test_resume_rejects_partial_execution_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            parent = self._frozen_parent(
                data_dir,
                self._reads(root),
                self._write_envs(root),
            ).model_copy(update={"lifecycle": "running"})
            save_tui_session_draft(parent)
            paths = SessionPaths(data_dir, parent.session_id)
            paths.ledger_events_path.parent.mkdir(parents=True, exist_ok=True)
            paths.ledger_events_path.write_text("partial\n", encoding="utf-8")
            stderr = io.StringIO()

            with (
                patch("app.entrypoints.run.execute_frozen_session") as execute,
                contextlib.redirect_stderr(stderr),
            ):
                code = _resume_tui_session(str(data_dir), parent.session_id)

            self.assertEqual(code, 1)
            self.assertIn("no resumable checkpoint", stderr.getvalue())
            execute.assert_not_called()
            self.assertEqual(
                load_tui_session_draft(data_dir, parent.session_id).lifecycle,
                "frozen",
            )

    def test_terminal_resume_creates_child_draft_without_changing_parent_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            envs = self._write_envs(root)
            parent = self._frozen_parent(data_dir, reads, envs)
            paths = SessionPaths(data_dir, parent.session_id)
            parent_hashes = {
                path.name: sha256_file(path)
                for path in (
                    paths.frozen_inputs_path,
                    paths.input_manifest_path,
                    paths.run_config_path,
                    paths.dependency_manifest_path,
                )
            }
            captured = {}

            def continue_child(child):
                captured["child"] = child
                return 0

            with (
                patch(
                    "app.entrypoints.tui._durable_supervision_state",
                    return_value="terminal",
                ),
                patch(
                    "app.entrypoints.run.generate_session_id",
                    return_value="child",
                ),
                patch(
                    "app.entrypoints.tui._parse_goal_with_configured_llm",
                    side_effect=RuntimeError("offline"),
                ),
                patch(
                    "app.entrypoints.tui._continue_tui_draft",
                    side_effect=continue_child,
                ),
                patch("builtins.input", side_effect=["y", "", ""]),
            ):
                code = _resume_tui_session(str(data_dir), parent.session_id)

            self.assertEqual(code, 0)
            child = captured["child"]
            self.assertEqual(child.session_id, "child")
            self.assertEqual(child.parent_session_id, parent.session_id)
            self.assertEqual(
                load_tui_session_draft(data_dir, "child").parent_session_id,
                parent.session_id,
            )
            for path in (
                paths.frozen_inputs_path,
                paths.input_manifest_path,
                paths.run_config_path,
                paths.dependency_manifest_path,
            ):
                self.assertEqual(parent_hashes[path.name], sha256_file(path))

    def test_confirmed_plan_without_tty_stays_editable_and_does_not_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="inventory only",
                intent=deterministic_workflow_intent("inventory only"),
            ).model_copy(update={"lifecycle": "ready_to_confirm"})
            save_tui_session_draft(draft)
            plan = AskPlanState(
                session_id="s1",
                data_dir=str(data_dir),
                entry_request={},
                manifest_ref=str(SessionPaths(data_dir, "s1").manifest_draft_path),
                selected_stage_occurrences=[],
                selected_methods={},
                confirmed=True,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=False,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = _freeze_and_execute(draft, plan)

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("real stdin/stdout TTY", stderr.getvalue())
            freeze.assert_not_called()
            execute.assert_not_called()
            self.assertFalse(SessionPaths(data_dir, "s1").frozen_inputs_path.exists())
            self.assertEqual(
                load_tui_session_draft(data_dir, "s1").lifecycle,
                "ready_to_confirm",
            )

    def test_confirmed_plan_freezes_then_calls_internal_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            envs = root / "envs.yaml"
            envs.write_text(
                "default:\n  fallback_env: test-env\n"
                "stages: {}\nmethods: {}\npackages: {}\n",
                encoding="utf-8",
            )
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )
            draft = apply_asset_answer(
                draft,
                answer=str(reads),
                focused_role="ont_reads",
            ).draft.model_copy(update={"lifecycle": "ready_to_confirm"})
            save_tui_session_draft(draft)
            plan = AskPlanState(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
                entry_request={"requested_stages": ["read_qc"]},
                manifest_ref=str(SessionPaths(data_dir, "s1").manifest_draft_path),
                selected_stage_occurrences=[
                    PlanStageOccurrence(stage="read_qc", allow_local_execution=True)
                ],
                selected_methods={
                    "read_qc": [
                        "read_qc.long_read_stats.seqkit_stats",
                        "read_qc.length_filter.skip",
                    ]
                },
                shared_parameters=SharedParameters(platform="ont", read_type="ont_raw"),
                confirmed=True,
            )

            with (
                patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": str(envs)},
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.entrypoints.run.execute_frozen_session",
                    return_value=0,
                ) as execute,
                patch(
                    "app.execution.run_readiness.preflight_run_readiness"
                ) as readiness,
            ):
                code = _freeze_and_execute(draft, plan)

            self.assertEqual(code, 0)
            paths = SessionPaths(data_dir, "s1")
            self.assertTrue(paths.input_manifest_path.is_file())
            self.assertTrue(paths.run_config_path.is_file())
            self.assertTrue(paths.dependency_manifest_path.is_file())
            self.assertTrue(paths.frozen_inputs_path.is_file())
            execute.assert_called_once_with(
                "s1",
                draft.data_dir,
                checkpointer="sqlite",
                interactive_gate=True,
            )
            readiness.assert_called_once()
            self.assertEqual(
                load_tui_session_draft(data_dir, "s1").lifecycle,
                "terminal",
            )

    def test_review_readiness_and_freeze_share_canonical_fixed_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            lineage = ensure_busco_lineage(root)
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="review",
                intent=deterministic_workflow_intent("review"),
            ).model_copy(update={"lifecycle": "ready_to_confirm"})
            save_tui_session_draft(draft)
            busco = {
                "lineage": lineage.name,
                "threads": 4,
            }
            plan = AskPlanState(
                session_id="s1",
                data_dir=str(data_dir),
                entry_request={},
                manifest_ref=str(SessionPaths(data_dir, "s1").manifest_draft_path),
                selected_stage_occurrences=[
                    PlanStageOccurrence(
                        stage="assembly_review",
                        allow_local_execution=True,
                    )
                ],
                selected_methods={"assembly_review": ["assembly_review.eval.quast"]},
                method_parameters={
                    "assembly_review.eval.quast": {"threads": 4},
                    "assembly_review.eval.busco": busco,
                },
                confirmed=True,
            )
            run_config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {
                            "stage": "assembly_review",
                            "allow_local_execution": True,
                        }
                    ],
                    "assembly_review": {
                        "quast": {"threads": 4},
                        "busco": busco,
                    },
                }
            )
            prepared = SimpleNamespace(
                manifest=SimpleNamespace(session_id="s1"),
                run_config=run_config,
            )

            with (
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch(
                    "app.execution.run_readiness.preflight_run_readiness"
                ) as readiness,
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch(
                    "app.entrypoints.run.execute_frozen_session",
                    return_value=0,
                ),
            ):
                code = _freeze_and_execute(draft, plan)

            self.assertEqual(code, 0)
            effective = {
                "assembly_review": (
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                )
            }
            readiness.assert_called_once_with(
                run_config,
                effective_methods=effective,
            )
            self.assertEqual(freeze.call_args.kwargs["selected_methods"], effective)

    def test_freeze_failure_points_to_bootstrap_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reads = self._reads(root)
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )
            draft = apply_asset_answer(
                draft,
                answer=str(reads),
                focused_role="ont_reads",
            ).draft
            save_tui_session_draft(draft)
            plan = AskPlanState(
                session_id="s1",
                data_dir=str(data_dir),
                entry_request={},
                manifest_ref=str(SessionPaths(data_dir, "s1").manifest_draft_path),
                selected_stage_occurrences=[],
                selected_methods={},
                confirmed=True,
            )
            stderr = io.StringIO()
            missing_config = root / "missing-envs.yaml"

            with (
                patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": str(missing_config)},
                ),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
                patch("app.entrypoints.run.execute_frozen_session") as execute,
                contextlib.redirect_stderr(stderr),
            ):
                code = _freeze_and_execute(draft, plan)

            self.assertEqual(code, 1)
            self.assertIn("bootstrap", stderr.getvalue())
            execute.assert_not_called()
            self.assertFalse(SessionPaths(data_dir, "s1").frozen_inputs_path.exists())

    def _write_envs(self, root: Path) -> Path:
        return ensure_test_host_envs(root)

    def _frozen_parent(self, data_dir: Path, reads: Path, envs: Path):
        lineage = envs.parent / "busco" / "embryophyta_odb10"
        parent = create_tui_session_draft(
            session_id="parent",
            data_dir=str(data_dir),
            request_text="assembly",
            intent=deterministic_workflow_intent("assembly"),
        )
        parent = apply_asset_answer(
            parent,
            answer=str(reads),
            focused_role="ont_reads",
        ).draft.model_copy(update={"lifecycle": "terminal"})
        save_tui_session_draft(parent)
        with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs)}):
            freeze_session_inputs(
                SessionPaths(data_dir, parent.session_id),
                manifest_draft=materialize_manifest_draft(parent),
                run_config_draft={
                    "requested_stages": [
                        {"stage": "assembly", "allow_local_execution": False},
                        {"stage": "assembly_review", "allow_local_execution": False},
                    ],
                    "assembly_review": {
                        "busco": {
                            "lineage": lineage.name,
                            "threads": 4,
                        }
                    },
                },
                config_path=envs,
            )
        return parent


if __name__ == "__main__":
    unittest.main()
