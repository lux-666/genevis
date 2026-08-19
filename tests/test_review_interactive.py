"""Tests for the interactive review CLI (gene-agent review)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.control.resume_transaction import (
    ResumeTransactionError,
    ResumeTransactionResult,
)
from app.entrypoints.review_interactive import (
    _prompt_choice,
    build_decision_brief,
    collect_gate_decision,
    prompt_assembly_review_decision,
    prompt_generic_gate_decision,
    prompt_hic_scaffolding_decision,
    render_blocked_summary,
    render_decision_brief,
    run_interactive_review,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactRef
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.storage.frozen_inputs import FrozenExecutionLease
from app.storage.ledger import Ledger
from tests.busco_fixture import assembly_review_config
from tests.data_v2 import v2_run_dir


def _primary_output_ref(candidate_id: str) -> dict:
    return ArtifactRef(
        artifact_id=f"artifact-{candidate_id}",
        path_ref=(
            "artifacts/steps/step-001-assembly_review/assembly_review/"
            f"{candidate_id}.fasta"
        ),
        sha256="a" * 64,
        size_bytes=10,
        artifact_type="assembly.draft_assembly",
    ).model_dump(mode="json")


def _review_occurrence_state(
    session_id: str, data_dir: str, stage: str
) -> dict[str, object]:
    """Build the minimum state needed to persist one non-inventory review gate."""

    return {
        "session_id": session_id,
        "data_dir": data_dir,
        "current_stage": stage,
        "workflow_steps": [
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": f"step-001-{stage}",
                "stage": stage,
                "allow_local_execution": False,
                "selected_method_names": (
                    [
                        "assembly_review.eval.quast",
                        "assembly_review.eval.busco",
                    ]
                    if stage == "assembly_review"
                    else []
                ),
                **(
                    {
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-000-inventory",
                        }
                    }
                    if stage == "assembly_review"
                    else {}
                ),
            },
        ],
        "workflow_step_index": 1,
    }


def _verified_inputs_for_review(stage: str) -> SimpleNamespace:
    payload: dict[str, object] = {
        "requested_stages": [{"stage": stage, "allow_local_execution": False}]
    }
    if stage == "assembly_review":
        payload["assembly_review"] = assembly_review_config(
            "/data/busco/embryophyta_odb10"
        )
    return SimpleNamespace(run_config=RunConfig.model_validate(payload))


def _assembly_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a blocked assembly-review session and return its ID and data dir."""
    session_id = "review-test-1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    draft_ref = "work/steps/step-001-assembly_review/assembly_review/flye/draft.fasta"
    draft_path = data_dir / draft_ref
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(">ctg1\nACGT\n", encoding="utf-8")
    payload = {
        "stage": "assembly_review",
        "gate_id": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {
                "candidate_id": "flye",
                "method_name": "assembly.candidate.flye",
                "primary_output_ref": _primary_output_ref("flye"),
            },
        ],
        "signals": [],
        "decisions_allowed": ["accept", "stop_human_review"],
        "notes": [],
        "report_refs": {},
        "candidate_comparison_context": {},
    }
    state = _review_occurrence_state(session_id, str(data_dir), "assembly_review")
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason=payload["reason"],
        payload=payload,
        method_name="assembly_review.contract",
        artifact_type="assembly_review.request",
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_stage_completed(
        session_id,
        "inventory",
        workflow_step_id="step-000-inventory",
    )
    ledger.record_review_requested(
        session_id,
        "assembly_review",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


def _hic_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a blocked Hi-C session and return its ID and data directory."""
    session_id = "review-test-hic"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    payload = {
        "stage": "hic_scaffolding",
        "gate_id": "hic_scaffolding",
        "reason": "autohic_adoption_review_required",
        "candidates": [{"method_name": "hic_scaffolding.candidate.autohic"}],
        "decisions_allowed": [
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ],
        "notes": ["Hi-C reads available"],
    }
    state = _review_occurrence_state(session_id, str(data_dir), "hic_scaffolding")
    persisted = persist_review_request(
        state,
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason=payload["reason"],
        payload=payload,
        method_name="hic_scaffolding.contract",
        artifact_type="hic_scaffolding.review_request",
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_stage_completed(
        session_id,
        "inventory",
        workflow_step_id="step-000-inventory",
    )
    ledger.record_review_requested(
        session_id,
        "hic_scaffolding",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


class PromptAssemblyReviewTests(unittest.TestCase):
    def test_parameter_approval_prompt_copies_exact_resume_bindings(self):
        bindings = {
            "attempt_id": 1,
            "task": "retry_after_execution_failure",
            "previous_attempt_id": 0,
            "recommendation_hash": "b" * 64,
        }
        requirements = {
            "stage": "assembly",
            "gate_id": "parameter_approval",
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
            "resume_metadata": bindings,
        }
        inputs = iter(["1", "approve exact recommendation"])

        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            decision = prompt_generic_gate_decision(requirements)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["decision"], "approve_parameters")
        self.assertEqual(decision["metadata"], bindings)

    def test_prompt_choice_retries_invalid_input(self):
        inputs = iter(["bad", "2"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            choice = _prompt_choice("decision", ["accept", "rerun"])
        self.assertEqual(choice, "rerun")

    def test_accept_single_candidate_auto_selects(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "pass"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(["1", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "flye")
        self.assertNotIn("final_assembly_ref", result)

    def test_decision_prompt_defers_to_frozen_workflow_sequence(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "stop_human_review"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(["1", ""])
        out = io.StringIO()
        with (
            patch("builtins.input", side_effect=lambda _: next(inputs)),
            patch("sys.stdout", out),
        ):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        rendered = out.getvalue()
        self.assertIn("remaining frozen workflow steps", rendered)
        self.assertNotIn("Hi-C scaffolding", rendered)

    def test_accept_multiple_candidates_prompts_choice(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept"],
            "candidate_ids": ["flye", "canu"],
            "candidate_primary_output_refs": {
                "flye": _primary_output_ref("flye"),
                "canu": _primary_output_ref("canu"),
            },
        }
        inputs = iter(["1", "2", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "canu")
        self.assertEqual(result["rejected_candidates"], ["flye"])

    def test_accept_shows_typed_candidates_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp) / "assembly_review.md")
            metric = str(Path(tmp) / "quast_summary.json")
            requirements = {
                "stage": "assembly_review",
                "decisions_allowed": ["accept"],
                "candidate_ids": ["flye", "nextdenovo"],
                "candidate_primary_output_refs": {
                    "flye": _primary_output_ref("flye"),
                    "nextdenovo": _primary_output_ref("nextdenovo"),
                },
                "candidate_report_refs": {"flye": [metric]},
                "report_refs": {"assembly_review_report": report},
            }
            inputs = iter(["1", "1", ""])
            out = io.StringIO()
            with (
                patch("builtins.input", side_effect=lambda _: next(inputs)),
                patch("sys.stdout", out),
            ):
                result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["candidate_id"], "flye")
        rendered = out.getvalue()
        self.assertIn("nextdenovo", rendered)
        self.assertIn("candidate_evidence", rendered)
        self.assertIn(
            "assembly.draft_assembly:artifact-flye",
            rendered,
        )
        self.assertIn(metric, rendered)
        self.assertIn(report, rendered)

    def test_cancel_on_eof_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        with patch("builtins.input", side_effect=EOFError):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)

    def test_stop_human_review_skips_candidate_selection(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "stop_human_review"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(["2", "stopping review"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "stop_human_review")
        self.assertIsNone(result["candidate_id"])

    def test_purge_collects_transform_metadata(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge", "accept"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(
            [
                "1",  # select purge
                "/path/purged.fasta",  # action_output_ref
                "purge_dups",  # method_name
                "purge_dups log shows 5% removed",  # evidence_notes
                "purged assembly",  # notes
            ]
        )
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "purge")
        self.assertEqual(result["candidate_id"], "flye")
        self.assertNotIn("final_assembly_ref", result)
        self.assertEqual(result["metadata"]["action_output_ref"], "/path/purged.fasta")
        self.assertTrue(result["metadata"]["human_approved_action"])
        self.assertEqual(result["metadata"]["method_name"], "purge_dups")
        self.assertIn(
            "purge_dups log shows 5% removed", result["metadata"]["evidence_notes"]
        )

    def test_purge_missing_evidence_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(
            [
                "1",  # select purge
                "/path/purged.fasta",  # action_output_ref
                "purge_dups",  # method_name
                "",  # empty evidence_notes → error
            ]
        )
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)

    def test_purge_missing_method_name_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge"],
            "candidate_ids": ["flye"],
            "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
        }
        inputs = iter(
            [
                "1",  # select purge
                "/path/purged.fasta",  # action_output_ref
                "",  # empty method_name → error
            ]
        )
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)


class PromptHicScaffoldingTests(unittest.TestCase):
    def test_accept_candidate_prompt_uses_operator_labels(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": [
                "accept_candidate",
                "keep_prior_assembly",
                "stop_human_review",
            ],
            "executed_candidate_methods": ["hic_scaffolding.candidate.autohic"],
        }
        inputs = iter(["1", ""])
        out = io.StringIO()

        def input_with_prompt(prompt):
            out.write(prompt)
            return next(inputs)

        with (
            patch("builtins.input", side_effect=input_with_prompt),
            patch("sys.stdout", out),
        ):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept_candidate")
        rendered = out.getvalue()
        self.assertIn("action:", rendered)
        self.assertIn("select action [1-3]", rendered)
        self.assertIn("accept scaffold result [accept_candidate]", rendered)
        self.assertIn("operator note (optional, audit only)", rendered)

    def test_keep_prior_assembly_needs_no_method_selection(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        inputs = iter(["2", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "keep_prior_assembly")
        self.assertNotIn("method_name", result)

    def test_keep_prior_assembly_decision(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        inputs = iter(["2", "no Hi-C data available"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "keep_prior_assembly")
        self.assertNotIn("method_name", result)
        self.assertEqual(result["notes"], ["no Hi-C data available"])

    def test_cancel_returns_none(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNone(result)


class RunInteractiveReviewTests(unittest.TestCase):
    def test_review_threads_one_verified_bundle_to_graph_and_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-bundle"
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, session_id)
            checkpoint_db = run_dir / "checkpoints" / "checkpoints.sqlite"
            checkpoint_db.write_text("", encoding="utf-8")
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                gate_id="assembly_review",
                decision="accept",
                candidate_id="flye",
            )
            verified_inputs = object()
            graph = object()
            transaction_result = ResumeTransactionResult(
                state={
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                },
                decision_artifact_path=None,
            )

            with (
                patch(
                    "app.control.lifecycle.inspect_session",
                    return_value={
                        "latest_review_status": {
                            "status": "awaiting_decision",
                            "accepts_new_decision": True,
                        },
                    },
                ),
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision",
                    return_value=(request, request.model_dump(mode="json")),
                ) as collect,
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=verified_inputs,
                ) as validate,
                patch(
                    "app.graph.builder.build_graph",
                    return_value=graph,
                ) as build,
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    return_value=transaction_result,
                ) as execute,
            ):
                code = run_interactive_review(session_id, str(data_root))

        self.assertEqual(code, 0)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(validate.call_args.args[0].run_dir, run_dir)
        self.assertIs(
            collect.call_args.kwargs["verified_inputs"],
            verified_inputs,
        )
        self.assertIs(build.call_args.kwargs["verified_inputs"], verified_inputs)
        self.assertIsInstance(execute.call_args.kwargs["lease"], FrozenExecutionLease)
        self.assertIn("projection_boundary", execute.call_args.kwargs)
        self.assertIn("projection_boundary", build.call_args.kwargs)

    def test_committed_intent_recovers_without_prompting_for_new_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-recovery"
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, session_id)
            checkpoint_db = run_dir / "checkpoints" / "checkpoints.sqlite"
            checkpoint_db.write_text("", encoding="utf-8")
            verified_inputs = object()
            graph = object()
            transaction_result = ResumeTransactionResult(
                state={
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": [],
                },
                decision_artifact_path=None,
            )

            with (
                patch(
                    "app.control.lifecycle.inspect_session",
                    return_value={
                        "latest_review_status": {
                            "status": "decision_committed",
                            "accepts_new_decision": False,
                        },
                    },
                ),
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision"
                ) as collect,
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=verified_inputs,
                ),
                patch("app.graph.builder.build_graph", return_value=graph),
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    return_value=transaction_result,
                ) as execute,
            ):
                code = run_interactive_review(session_id, str(data_root))

        self.assertEqual(code, 0)
        collect.assert_not_called()
        self.assertIsNone(execute.call_args.kwargs["request"])
        self.assertEqual(
            execute.call_args.kwargs["source"],
            "interactive_review_recovery",
        )

    def test_session_not_found_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "nonexistent"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_not_blocked_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "completed-session"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            ledger = Ledger(data_dir)
            ledger.create_session(session_id, "control/manifest.yaml")
            code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_wrong_supervision_state_does_not_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-wrong-state"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "completed",
                "current_stage": "assembly_review",
                "stage_outputs": {"assembly_review": {"status": "completed"}},
                "route_trace": [],
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type(
                        "Snapshot", (), {"values": state}
                    )(),
                    "update_state": lambda self, config, update: (_ for _ in ()).throw(
                        AssertionError("should not update")
                    ),
                    "invoke": lambda self, state_arg, config: (_ for _ in ()).throw(
                        AssertionError("should not invoke")
                    ),
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
            )
            raw_decision = request.model_dump(mode="json")
            with (
                patch(
                    "app.control.lifecycle.inspect_session",
                    return_value={
                        "latest_review_status": {
                            "status": "awaiting_decision",
                            "accepts_new_decision": True,
                        },
                    },
                ),
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision",
                    return_value=(request, raw_decision),
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
            ):
                code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_cancelled_prompt_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _assembly_blocked_session(tmp)
            with patch("builtins.input", side_effect=EOFError):
                code = run_interactive_review(session_id, data_dir)
            self.assertEqual(code, 1)

    def test_update_state_failure_does_not_invoke_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-update-failure"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "control_state": "running",
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "input_manifest_ref": "control/manifest.yaml",
                "active_interrupt": {
                    "stage": "assembly_review",
                    "gate_id": "assembly_review",
                },
                "review_notes": [],
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "route_trace": [],
                "skip_reasons": {},
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type(
                        "Snapshot", (), {"values": state}
                    )(),
                    "update_state": lambda self, config, update: (_ for _ in ()).throw(
                        RuntimeError("update boom")
                    ),
                    "invoke": lambda self, state_arg, config: (_ for _ in ()).throw(
                        AssertionError("should not invoke")
                    ),
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
            )
            raw_decision = request.model_dump(mode="json")
            decision_path = (
                data_dir / "artifacts" / "reviews" / "review_test" / "decision.json"
            )
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            decision_path.write_text("{}\n", encoding="utf-8")
            with (
                patch(
                    "app.control.lifecycle.inspect_session",
                    return_value={
                        "latest_review_status": {
                            "status": "awaiting_decision",
                            "accepts_new_decision": True,
                        },
                    },
                ),
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision",
                    return_value=(request, raw_decision),
                ),
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=SimpleNamespace(),
                ),
                patch("app.graph.builder.build_graph", return_value=graph),
                patch(
                    "app.control.resume_transaction.execute_resume_transaction",
                    side_effect=ResumeTransactionError(
                        "checkpoint_apply",
                        "update boom",
                        review_status="application_prepared",
                    ),
                ) as execute,
            ):
                code = run_interactive_review(session_id, str(data_dir))

            self.assertEqual(code, 1)
            execute.assert_called_once()

    def test_dry_run_rejects_invalid_frozen_inputs_before_any_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-invalid-frozen"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            stderr = io.StringIO()

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    side_effect=ValueError("frozen input hash mismatch"),
                ),
                patch(
                    "app.entrypoints.review_interactive.collect_gate_decision"
                ) as collect,
                patch(
                    "app.entrypoints.review_interactive.persist_decision_artifact"
                ) as persist,
                patch("sys.stderr", stderr),
            ):
                code = run_interactive_review(
                    session_id,
                    str(data_dir),
                    dry_run=True,
                )

        self.assertEqual(code, 1)
        self.assertIn("frozen input hash mismatch", stderr.getvalue())
        collect.assert_not_called()
        persist.assert_not_called()

    def test_dry_run_writes_decision_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _assembly_blocked_session(tmp)
            inputs = iter(["1", ""])
            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=_verified_inputs_for_review("assembly_review"),
                ) as validate,
                patch("builtins.input", side_effect=lambda _: next(inputs)),
            ):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            self.assertGreaterEqual(validate.call_count, 1)
            decision_dir = Path(data_dir) / "work" / "steps"
            decisions = list(decision_dir.glob("*/*/reviews/*/decision.preview.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "accept")
            self.assertEqual(decision["candidate_id"], "flye")
            self.assertEqual(decision["decided_by"], "human")

    def test_hic_dry_run_keep_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _hic_blocked_session(tmp)
            inputs = iter(["2", "skipping scaffolding"])
            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=_verified_inputs_for_review("hic_scaffolding"),
                ) as validate,
                patch("builtins.input", side_effect=lambda _: next(inputs)),
            ):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            self.assertGreaterEqual(validate.call_count, 1)
            decision_dir = Path(data_dir) / "work" / "steps"
            decisions = list(decision_dir.glob("*/*/reviews/*/decision.preview.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "keep_prior_assembly")
            self.assertEqual(decision["stage"], "hic_scaffolding")

    def test_gate_decision_prefers_checkpoint_state_to_ledger_inference(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "inventory-blocked"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            payload = {
                "stage": "inventory",
                "gate_id": "inventory_review",
                "reason": "review_required",
                "messages": ["raw_long_reads: platform unknown"],
                "decisions_allowed": ["continue", "stop_human_review"],
            }
            state = initial_state(
                session_id=session_id,
                data_dir=str(data_dir),
                input_manifest_ref="control/manifest.yaml",
            )
            state.update(
                {
                    "control_state": "running",
                    "supervision_state": "blocked_human",
                    "stage_outputs": {
                        "inventory": {
                            "status": "validated",
                            "valid": True,
                            "warnings": ["raw_long_reads: platform unknown"],
                        }
                    },
                }
            )
            persisted = persist_review_request(
                state,
                stage="inventory",
                gate_id="inventory_review",
                reason=payload["reason"],
                payload=payload,
            )
            state["active_interrupt"] = persisted.compact_interrupt(
                stage="inventory",
                gate_id="inventory_review",
                reason=payload["reason"],
            )
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type(
                        "Snapshot", (), {"values": state}
                    )()
                },
            )()
            inputs = iter(["1", "continue inventory"])

            with (
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=SimpleNamespace(),
                ),
                patch("app.graph.builder.build_graph", return_value=graph),
                patch("builtins.input", side_effect=lambda _: next(inputs)),
            ):
                result = collect_gate_decision(session_id, str(data_dir))

            self.assertIsNotNone(result)
            request, raw = result
            self.assertEqual(request.stage, "inventory")
            self.assertEqual(request.gate_id, "inventory_review")
            self.assertEqual(request.decision, "continue")
            self.assertEqual(raw["notes"], ["continue inventory"])


class RenderBlockedSummaryTests(unittest.TestCase):
    def test_renders_assembly_review_without_error(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
                "report_refs": {"report": "/tmp/report.json"},
                "candidate_comparison_context": {"flye_n50": 10000},
            },
            "latest_review_request_artifact": {"file_path": "/tmp/request.json"},
            "next_action": {"relevant_refs": ["/tmp/ref"]},
        }
        render_blocked_summary(summary)

    def test_renders_assembly_review_following_frozen_occurrences(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
            },
            "workflow_steps": [
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
                    "allow_local_execution": True,
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
                },
                {
                    "step_id": "step-004-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": True,
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-003-t2t",
                    },
                },
            ],
            "workflow_step_index": 2,
            "latest_review_request_artifact": None,
            "next_action": {},
        }
        out = io.StringIO()
        with patch("sys.stdout", out):
            render_blocked_summary(summary)

        rendered = out.getvalue()
        self.assertIn("after_accepting_assembly", rendered)
        self.assertIn(
            "next: step-003-t2t (t2t)",
            rendered,
        )
        self.assertIn("runtime does not append or reorder them", rendered)

    def test_renders_hic_scaffolding_without_error(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "hic_scaffolding",
                "decisions_allowed": ["select_method", "skip"],
                "candidate_methods": ["hic_scaffolding.candidate.autohic"],
            },
            "latest_review_request_artifact": None,
            "next_action": {},
        }
        render_blocked_summary(summary)


class ValidationTests(unittest.TestCase):
    def test_removed_final_assembly_ref_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResumeReviewRequest.model_validate(
                {
                    "session_id": "s1",
                    "stage": "assembly_review",
                    "decision": "accept",
                    "candidate_id": "flye",
                    "final_assembly_ref": "draft.fasta",
                    "decided_by": "human",
                }
            )

    def test_unknown_decision_value_is_schema_valid(self):
        raw = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "invalid_decision",
            "candidate_id": "flye",
            "decided_by": "human",
        }
        request = ResumeReviewRequest.model_validate(raw)
        self.assertEqual(request.decision, "invalid_decision")

    def test_unsupported_stage_fails(self):
        raw = {
            "session_id": "s1",
            "stage": "genome_survey",
            "decision": "accept",
            "decided_by": "human",
        }
        request = ResumeReviewRequest.model_validate(raw)
        self.assertEqual(request.stage, "genome_survey")


class DecisionBriefTests(unittest.TestCase):
    def test_parameter_approval_brief_renders_diff_failure_and_top5_paper_evidence(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            basis_path = root / "basis_snapshot.json"
            basis_path.write_text(
                json.dumps(
                    {
                        "literature_cases": [
                            {
                                "case_id": f"paper-{index}",
                                "source": {"doi": f"10.1000/{index}"},
                                "source_excerpts": [
                                    {
                                        "span_id": f"methods-{index}",
                                        "quote": f"Exact methods quote {index}.",
                                    }
                                ],
                            }
                            for index in range(6)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            recommendation_path = root / "validated_recommendation.json"
            recommendation_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "method_id": "hifiasm",
                        "task": "retry_after_execution_failure",
                        "recommendation": {"params": {"purge_level": 2}},
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = root / "recommendation_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "refs": {
                            "basis_snapshot_ref": {"path_ref": str(basis_path)},
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = {
                "session_id": "s1",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "assembly",
                    "gate_id": "parameter_approval",
                    "decisions_allowed": ["approve_parameters", "reject_parameters"],
                    "resume_metadata": {
                        "attempt_id": 1,
                        "task": "retry_after_execution_failure",
                        "previous_attempt_id": 0,
                        "recommendation_hash": "d" * 64,
                    },
                    "recommendation_manifest_ref": {"path_ref": str(manifest_path)},
                    "recommendation_ref": {"path_ref": str(recommendation_path)},
                    "parameter_diff": {"purge_level": {"from": 1, "to": 2}},
                    "rationale": "retry after OOM",
                    "failure_facts": {"signals": {"return_code": 137}},
                    "evidence_refs": {"basis_snapshot_ref": str(basis_path)},
                },
                "latest_review_request_artifact": None,
                "next_action": {},
            }

            brief = build_decision_brief(summary, tmp)
            out = io.StringIO()
            with patch("sys.stdout", out):
                render_decision_brief(brief)

        proposal = brief["parameter_proposal"]
        self.assertEqual(proposal["parameter_diff"]["purge_level"]["to"], 2)
        self.assertEqual(proposal["failure_facts"]["signals"]["return_code"], 137)
        self.assertEqual(len(proposal["literature_cases"]), 5)
        rendered = out.getvalue()
        self.assertIn("production paper evidence (5, top-5 maximum)", rendered)
        self.assertIn("doi=10.1000/0", rendered)
        self.assertIn("Exact methods quote 0.", rendered)

    def test_assembly_review_brief_structure(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_primary_output_refs": {"flye": _primary_output_ref("flye")},
                "report_refs": {"quast": "/tmp/quast_report.json"},
                "candidate_comparison_context": {
                    "flye_n50": 50000,
                    "flye_total_length": 500000000,
                },
            },
            "latest_review_request_artifact": {"file_path": "/tmp/request.json"},
            "next_action": {"relevant_refs": ["/tmp/ref1"]},
        }
        brief = build_decision_brief(summary, "/tmp/data")

        self.assertEqual(brief["session_id"], "s1")
        self.assertEqual(brief["stage"], "assembly_review")
        self.assertEqual(brief["supervision_state"], "blocked_human")
        self.assertEqual(brief["decisions_allowed"], ["accept", "stop_human_review"])
        self.assertEqual(brief["review_request_ref"], "/tmp/request.json")
        self.assertEqual(len(brief["candidates"]), 1)
        self.assertEqual(brief["candidates"][0]["id"], "flye")
        self.assertEqual(
            brief["candidates"][0]["ref"],
            "assembly.draft_assembly:artifact-flye",
        )
        self.assertNotIn("exists", brief["candidates"][0])
        self.assertEqual(brief["evidence"]["metrics"]["flye_n50"], 50000)
        self.assertEqual(
            brief["evidence"]["report_refs"]["quast"], "/tmp/quast_report.json"
        )
        self.assertEqual(brief["relevant_refs"], ["/tmp/ref1"])

    def test_assembly_brief_with_request_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "request.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "stage": "assembly_review",
                        "candidates": [
                            {
                                "candidate_id": "flye",
                                "method_name": "assembly.candidate.flye",
                                "status": "completed",
                            },
                        ],
                        "notes": ["N50 above threshold"],
                        "signals": ["busco_complete_above_90"],
                    }
                ),
                encoding="utf-8",
            )

            summary = {
                "session_id": "s2",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "assembly_review",
                    "decisions_allowed": ["accept"],
                    "candidate_ids": ["flye"],
                    "candidate_primary_output_refs": {
                        "flye": _primary_output_ref("flye")
                    },
                    "report_refs": {},
                    "candidate_comparison_context": {},
                },
                "latest_review_request_artifact": {"file_path": str(artifact_path)},
                "next_action": {},
            }
            brief = build_decision_brief(summary, tmp)

            self.assertEqual(
                brief["candidates"][0]["method"], "assembly.candidate.flye"
            )
            self.assertEqual(brief["candidates"][0]["status"], "completed")
            self.assertIn("N50 above threshold", brief["evidence"]["notes"])
            self.assertIn("busco_complete_above_90", brief["evidence"]["notes"])

    def test_hic_scaffolding_brief_structure(self):
        summary = {
            "session_id": "s3",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "hic_scaffolding",
                "decisions_allowed": ["select_method", "skip"],
                "candidate_methods": ["hic_scaffolding.candidate.autohic"],
            },
            "latest_review_request_artifact": None,
            "latest_review_request": {
                "payload": {"pre_hic_assembly_ref": "/tmp/pre_hic.fasta"},
            },
            "next_action": {},
        }
        brief = build_decision_brief(summary, "/tmp/data")

        self.assertEqual(brief["stage"], "hic_scaffolding")
        self.assertEqual(len(brief["candidates"]), 1)
        self.assertEqual(
            brief["candidates"][0]["method"], "hic_scaffolding.candidate.autohic"
        )
        self.assertEqual(
            brief["evidence"]["report_refs"]["pre_hic_assembly_ref"],
            "/tmp/pre_hic.fasta",
        )

    def test_hic_brief_with_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "hic_request.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "stage": "hic_scaffolding",
                        "candidates": [
                            {"method_name": "hic_scaffolding.candidate.autohic"}
                        ],
                        "notes": [
                            "Hi-C reads available",
                            "Contact map review required",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = {
                "session_id": "s4",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "hic_scaffolding",
                    "decisions_allowed": ["select_method", "skip"],
                    "candidate_methods": ["hic_scaffolding.candidate.autohic"],
                },
                "latest_review_request_artifact": {"file_path": str(artifact_path)},
                "latest_review_request": {"payload": {}},
                "next_action": {},
            }
            brief = build_decision_brief(summary, tmp)

            self.assertIn("Hi-C reads available", brief["evidence"]["notes"])
            self.assertIn("Contact map review required", brief["evidence"]["notes"])

    def test_render_decision_brief_no_crash(self):
        brief = {
            "session_id": "s1",
            "stage": "assembly_review",
            "supervision_state": "blocked_human",
            "decisions_allowed": ["accept", "pass"],
            "review_request_ref": "/tmp/request.json",
            "runtime_proposal": {
                "kind": "assembly_candidate_review",
                "label": "Assembly result and evidence review",
                "parameter_recommendation_status": "mixed",
                "related_parameter_gate": {
                    "stage": "assembly",
                    "gate_id": "parameter_approval",
                },
                "parameter_recommendation_notes": [
                    "Assembly parameter retries use the v3 recommendation "
                    "and approval workflow."
                ],
                "human_confirmation_required": True,
            },
            "candidates": [
                {
                    "id": "flye",
                    "ref": "/tmp/draft.fasta",
                    "method": "assembly.candidate.flye",
                    "exists": False,
                },
            ],
            "evidence": {
                "metrics": {"flye_n50": 50000},
                "report_refs": {"quast": "/tmp/quast.json"},
                "notes": ["N50 above threshold"],
            },
            "relevant_refs": ["/tmp/ref1"],
        }
        render_decision_brief(brief)

    def test_t2t_runtime_proposal_reports_parameter_recommender(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "t2t",
                "gate_id": "t2t_review",
                "decisions_allowed": ["continue_without_patch"],
                "runtime_proposal": {
                    "kind": "finishing_candidate_review",
                    "label": "T2T finishing candidate review",
                    "parameter_recommendation_status": "implemented",
                    "human_confirmation_required": True,
                },
            },
            "latest_review_request_artifact": None,
            "next_action": {},
        }

        brief = build_decision_brief(summary, "/tmp")
        out = io.StringIO()
        with patch("sys.stdout", out):
            render_decision_brief(brief)

        self.assertEqual(
            brief["runtime_proposal"]["parameter_recommendation_status"], "implemented"
        )
        self.assertIn("parameter recommendation: implemented", out.getvalue())

    def test_empty_brief_renders_without_error(self):
        brief = {
            "session_id": "s1",
            "stage": "unknown",
            "decisions_allowed": [],
        }
        render_decision_brief(brief)


if __name__ == "__main__":
    unittest.main()
