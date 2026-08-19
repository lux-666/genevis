"""End-to-end tests for the Phase 5/02 durable resume CLI.

These tests build a real parent graph against a real sqlite checkpointer,
seed a blocked-at-assembly_review state, exit the process by closing the
saver/connection, then reopen and resume via the CLI's _run_resume helper.

The tests verify that:
- A blocked state survives across SqliteSaver close/reopen cycles.
- _run_resume validates the decision file before mutating any state.
- A valid resume advances the workflow past assembly_review and writes a
  review_decision ledger event.
- An invalid resume leaves the durable state untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.control.workflow_plan import workflow_steps_from_run_config
from app.entrypoints.run import _run_resume
from app.graph.builder import build_graph
from app.graph.checkpointer import checkpoint_db_path
from app.review.base import ReviewPayload
from app.review.persistence import PersistedReviewRequest, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.storage.artifacts import publish_artifact_record
from app.storage.frozen_inputs import (
    FrozenInputBundle,
    freeze_session_inputs,
    validate_frozen_inputs,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.busco_fixture import assembly_review_config, ensure_busco_lineage
from tests.data_v2 import v2_run_dir


def _freeze_durable_inputs(
    run_dir: Path, *, include_downstream_stage: bool = False
) -> FrozenInputBundle:
    paths = SessionPaths.from_run_dir(run_dir)
    reads = paths.run_dir / "fixture_reads.fastq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    paths.manifest_path.write_text(
        json.dumps(
            {
                "session_id": paths.session_id,
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(reads)],
                        "platform": "ont",
                        "format": "fastq",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    envs = paths.control_dir / "test_envs.yaml"
    envs.write_text(
        "default:\n  fallback_env: test-env\npackages: {}\n",
        encoding="utf-8",
    )
    requested_stages = [
        {"stage": "assembly", "allow_local_execution": False},
        {"stage": "assembly_review", "allow_local_execution": False},
    ]
    if include_downstream_stage:
        requested_stages.append(
            {"stage": "base_polish", "allow_local_execution": False}
        )
    lineage = ensure_busco_lineage(paths.run_dir)
    run_config = RunConfig.model_validate(
        {
            "requested_stages": requested_stages,
            "assembly_review": assembly_review_config(lineage),
        }
    )
    freeze_session_inputs(
        paths,
        manifest_draft=paths.manifest_path,
        run_config_draft=run_config,
        config_path=envs,
        probe_environments=False,
    )
    return validate_frozen_inputs(paths)


def _build_frozen_graph(run_dir: Path):
    return build_graph(
        backend="sqlite",
        data_dir=str(run_dir),
        verified_inputs=validate_frozen_inputs(SessionPaths.from_run_dir(run_dir)),
    )


def _build_blocked_state(
    run_dir: Path,
    *,
    session_id: str = "s1",
    include_downstream_stage: bool = False,
) -> tuple[dict, PersistedReviewRequest, Path]:
    paths = SessionPaths.from_run_dir(run_dir)
    verified_inputs = _freeze_durable_inputs(
        run_dir,
        include_downstream_stage=include_downstream_stage,
    )
    assembly_step_id = "step-001-assembly"
    draft_path = paths.workflow_step_work_path(
        assembly_step_id, "assembly", "draft.fasta"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(">c\nACGT\n", encoding="utf-8")
    candidate_id = "flye"
    method_name = "assembly.candidate.flye"
    candidate_dir = (
        paths.workflow_step_artifacts_dir(assembly_step_id, "assembly") / candidate_id
    )
    primary = publish_artifact_record(
        run_dir,
        session_id,
        ArtifactMeta.from_file(
            draft_path,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        ),
        destination=candidate_dir / "draft.fasta",
    )
    planned = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=method_name,
        status="planned",
        input_type="raw_long_reads",
        input_member_ids={"primary": ["flye:reads"]},
        input_member_platforms={"primary": ["ont"]},
        parameter_attempt_id=0,
        expected_outputs=["primary_assembly"],
    )
    planned_path = paths.workflow_step_work_path(
        assembly_step_id, "assembly", candidate_id, "candidate.planned.json"
    )
    planned_path.parent.mkdir(parents=True, exist_ok=True)
    planned_path.write_text(
        json.dumps(planned.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_plan = publish_artifact_record(
        run_dir,
        session_id,
        ArtifactMeta.from_file(
            planned_path,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.planned.json",
    )
    executed = planned.model_copy(
        update={
            "status": "executed",
            "plan_ref": published_plan.ref,
            "primary_output": primary.ref,
        }
    )
    executed_path = paths.workflow_step_work_path(
        assembly_step_id, "assembly", candidate_id, "candidate.executed.json"
    )
    executed_path.write_text(
        json.dumps(executed.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_executed = publish_artifact_record(
        run_dir,
        session_id,
        ArtifactMeta.from_file(
            executed_path,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.executed.json",
    )
    candidates = [
        {
            "candidate_id": candidate_id,
            "method_name": method_name,
            "source": "test_fixture",
            "status": "executed",
            "bundle_ref": published_executed.ref.model_dump(mode="json"),
            "primary_output_ref": primary.ref.model_dump(mode="json"),
        }
    ]
    reason = "assembly_review_human_decision_required"
    payload = ReviewPayload(
        stage="assembly_review",
        gate_id="assembly_review",
        reason=reason,
        candidates=candidates,
        decisions_allowed=["accept", "stop_human_review"],
    )
    state = initial_state(
        session_id=session_id,
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        data_dir=str(run_dir),
        workflow_steps=workflow_steps_from_run_config(verified_inputs.run_config),
        stage_plan_ref=paths.to_ref(paths.run_config_path),
    )
    state.update(
        {
            "current_stage": "assembly_review",
            "workflow_step_index": 2,
            "entered_workflow_step_index": 2,
        }
    )
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason=reason,
        payload=payload,
        method_name="assembly_review.contract",
        artifact_type="assembly_review.request",
    )
    ledger = Ledger(run_dir)
    ledger.create_session(session_id, paths.to_ref(paths.input_manifest_path))
    ledger.record_stage_completed(
        session_id,
        "inventory",
        workflow_step_id="step-000-inventory",
    )
    ledger.record_resource_delivered(
        session_id,
        stage="assembly",
        workflow_step_id=assembly_step_id,
        resource_type="assembly",
        delivery_mode="materialized",
        candidate_bundle_ref=published_executed.ref,
        primary_output_ref=primary.ref,
    )
    ledger.record_stage_completed(
        session_id,
        "assembly",
        workflow_step_id=assembly_step_id,
    )
    for evaluator in (
        "assembly_review.eval.quast",
        "assembly_review.eval.busco",
    ):
        ledger.record_method_disposition(
            session_id,
            "assembly_review",
            "step-002-assembly_review",
            evaluator,
            "skipped",
            reason="local_execution_not_authorized",
        )
    ledger.record_review_requested(
        session_id,
        "assembly_review",
        reason=reason,
        candidates=candidates,
        review_request_artifact_ref=persisted.request_ref,
        review_request_id=persisted.review_request_id,
    )
    state.update(
        {
            "supervision_state": "blocked_human",
            "current_stage": "assembly_review",
            "active_interrupt": persisted.compact_interrupt(
                stage="assembly_review",
                gate_id="assembly_review",
                reason=reason,
            ),
            "stage_outputs": {
                "assembly_review": {
                    "status": "blocked_human",
                    "candidates": candidates,
                    "rejected_candidates": {},
                    "review_request_artifact_ref": persisted.request_ref.model_dump(
                        mode="json"
                    ),
                }
            },
        }
    )
    return state, persisted, draft_path


def _seed_blocked_checkpoint(data_dir: Path, state: dict) -> None:
    """Persist `state` into a sqlite checkpoint without running the graph.

    F12: earlier this helper called ``graph.invoke(state, cfg)`` and
    relied on ``route_controller`` exiting straight to END whenever
    ``supervision_state == "blocked_human"``. That coupled the test
    fixture to a specific routing implementation; future routing
    changes (e.g. recording a route trace event when blocked) would
    leak into the seeded ledger and confuse downstream assertions.

    Writing the state directly via ``graph.update_state`` bypasses
    routing entirely and produces a checkpoint with the same shape as
    a real blocked run.
    """
    graph = _build_frozen_graph(data_dir)
    cfg = {"configurable": {"thread_id": state["session_id"]}}
    graph.update_state(cfg, state)
    saver = graph.checkpointer
    if hasattr(saver, "conn"):
        saver.conn.close()


def _capture(func, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(*args)
    return code, out.getvalue(), err.getvalue()


class DurableResumeTests(unittest.TestCase):
    def test_durable_resume_assembly_review_accept_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            state, persisted, draft_path = _build_blocked_state(run_dir)
            _seed_blocked_checkpoint(run_dir, state)

            decision_path = run_dir / "control" / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "gate_id": "assembly_review",
                        "review_request_id": persisted.review_request_id,
                        "review_request_hash": persisted.request_hash,
                        "decision": "accept",
                        "candidate_id": "flye",
                        "notes": ["operator accepted in test"],
                    }
                ),
                encoding="utf-8",
            )

            code, stdout, stderr = _capture(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision_path),
                    data_dir=str(data_root),
                    checkpointer="sqlite",
                ),
            )

            # The graph may exit non-zero if a downstream stage (e.g.
            # base_polish) hits its own gate; what matters here is
            # that the resume itself happened — assembly_review accepted,
            # the workflow advanced, and a review_decision ledger event
            # was written before whatever downstream gate may have
            # rejected execution.
            self.assertIn("session_id: s1", stdout)
            self.assertIn("checkpoint_persistence: sqlite", stdout)
            # N5: regression guard — no Python traceback should leak to
            # stderr regardless of which downstream stage rejected the
            # advanced state.
            self.assertNotIn("Traceback", stderr)

            connection = sqlite3.connect(checkpoint_db_path(run_dir))
            try:
                checkpoint_rows = connection.execute(
                    "SELECT checkpoint_ns, parent_checkpoint_id FROM checkpoints "
                    "WHERE thread_id = ?",
                    ("s1",),
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(checkpoint_rows, [("", None)])

            ledger_events = Ledger(run_dir).read_events("s1")
            self.assertTrue(
                any(
                    event["event_type"] == "terminal_compaction_completed"
                    for event in ledger_events
                )
            )
            decision_events = [
                e for e in ledger_events if e["event_type"] == "review_decision"
            ]
            self.assertTrue(decision_events, "expected review_decision ledger event")
            # The accepted assembly_review decision advances into downstream
            # finishing gates; assert on the assembly_review decision specifically
            # rather than the last one.
            assembly_decisions = [
                e for e in decision_events if e["stage"] == "assembly_review"
            ]
            self.assertTrue(
                assembly_decisions,
                "expected assembly_review review_decision ledger event",
            )
            self.assertEqual(assembly_decisions[-1]["decision"], "accept")
            self.assertEqual(assembly_decisions[-1]["candidate_id"], "flye")

            # F3: lifecycle continuation events around the resume.
            resume_started = [
                e for e in ledger_events if e["event_type"] == "resume_started"
            ]
            resume_completed = [
                e for e in ledger_events if e["event_type"] == "resume_completed"
            ]
            self.assertEqual(len(resume_started), 1)
            self.assertEqual(len(resume_completed), 1)
            self.assertEqual(resume_started[0]["source"], "cli")
            self.assertEqual(
                resume_started[0]["decision_file_ref"],
                SessionPaths.from_run_dir(run_dir).to_ref(decision_path),
            )
            self.assertEqual(resume_started[0]["decision"]["stage"], "assembly_review")
            self.assertEqual(resume_started[0]["decision"]["decision"], "accept")
            # N3: resume events should record backend + data_dir so the
            # ledger trail distinguishes between sqlite and memory runs.
            self.assertEqual(resume_started[0]["checkpointer"], "sqlite")
            self.assertEqual(
                Path(resume_started[0]["data_dir"]).resolve(strict=False),
                run_dir.resolve(strict=False),
            )
            self.assertEqual(resume_completed[0]["checkpointer"], "sqlite")
            self.assertIn(
                resume_completed[0]["supervision_state"],
                {"running_stage", "completed", "failed_terminal", "blocked_human"},
            )
            self.assertIn("current_stage", resume_completed[0])
            # resume_started must precede review_decision and resume_completed
            event_types = [e["event_type"] for e in ledger_events]
            self.assertLess(
                event_types.index("resume_started"),
                event_types.index("review_decision"),
            )
            self.assertLess(
                event_types.index("review_decision"),
                event_types.index("resume_completed"),
            )

            graph = _build_frozen_graph(run_dir)
            snap = graph.get_state({"configurable": {"thread_id": "s1"}})
            advanced_past_assembly_review = snap.values["current_stage"] not in {
                "assembly_review"
            }
            self.assertTrue(
                advanced_past_assembly_review,
                "expected current_stage past assembly_review, "
                f"got {snap.values['current_stage']!r}",
            )
            if snap.values["supervision_state"] == "blocked_human":
                active = snap.values.get("active_interrupt") or {}
                self.assertNotEqual(
                    active.get("stage"),
                    "assembly_review",
                    "the assembly_review block should be cleared",
                )
            if hasattr(graph.checkpointer, "conn"):
                graph.checkpointer.conn.close()

    def test_invalid_decision_leaves_blocked_state_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            state, persisted, _draft_path = _build_blocked_state(run_dir)
            _seed_blocked_checkpoint(run_dir, state)

            decision_path = run_dir / "control" / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "gate_id": "assembly_review",
                        "review_request_id": persisted.review_request_id,
                        "review_request_hash": persisted.request_hash,
                        "decision": "accept",
                        "candidate_id": "unknown_candidate",
                    }
                ),
                encoding="utf-8",
            )

            code, _stdout, stderr = _capture(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision_path),
                    data_dir=str(data_root),
                    checkpointer="sqlite",
                ),
            )
            self.assertEqual(code, 1)
            self.assertIn("resume rejected", stderr)

            graph = _build_frozen_graph(run_dir)
            snap = graph.get_state({"configurable": {"thread_id": "s1"}})
            self.assertEqual(snap.values["supervision_state"], "blocked_human")
            self.assertEqual(snap.values["current_stage"], "assembly_review")
            self.assertIsNotNone(snap.values["active_interrupt"])
            if hasattr(graph.checkpointer, "conn"):
                graph.checkpointer.conn.close()

            ledger_events = Ledger(run_dir).read_events("s1")
            decision_events = [
                e for e in ledger_events if e["event_type"] == "review_decision"
            ]
            self.assertEqual(
                decision_events,
                [],
                "no review_decision should be written for an invalid resume",
            )
            paths = SessionPaths.from_run_dir(run_dir)
            intent_path = paths.workflow_step_artifact_path(
                "step-002-assembly_review",
                "assembly_review",
                "reviews",
                persisted.review_request_id,
                "intent.json",
            )
            self.assertFalse(
                intent_path.exists(),
                "invalid resume must not commit intent",
            )
            # F3: an invalid decision should still emit resume_started +
            # resume_failed so the audit trail records the attempt.
            started = [e for e in ledger_events if e["event_type"] == "resume_started"]
            failed = [e for e in ledger_events if e["event_type"] == "resume_failed"]
            self.assertEqual(len(started), 1)
            self.assertEqual(len(failed), 1)
            self.assertIn("error", failed[0])

    def test_resume_with_no_thread_in_checkpoint_db_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            state, persisted, draft_path = _build_blocked_state(run_dir)
            # Seed under a different thread_id so the resume target is missing.
            seeded = dict(state)
            seeded["session_id"] = "other"
            seeded["thread_id"] = "other"
            _seed_blocked_checkpoint(run_dir, seeded)

            decision_path = run_dir / "control" / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "gate_id": "assembly_review",
                        "review_request_id": persisted.review_request_id,
                        "review_request_hash": persisted.request_hash,
                        "decision": "accept",
                        "candidate_id": "flye",
                    }
                ),
                encoding="utf-8",
            )

            code, _stdout, stderr = _capture(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision_path),
                    data_dir=str(data_root),
                    checkpointer="sqlite",
                ),
            )
            self.assertEqual(code, 1)
            self.assertIn("no checkpoint found", stderr)

    def test_resume_invoke_exception_returns_exit_code_not_traceback(self):
        # N2: when graph.invoke raises during resume, _run_resume must
        # log a resume_failed ledger event AND return exit code 1
        # rather than letting the traceback escape to the terminal.
        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            run_dir = v2_run_dir(data_root, "s1")
            state, persisted, draft_path = _build_blocked_state(
                run_dir,
                include_downstream_stage=True,
            )
            _seed_blocked_checkpoint(run_dir, state)

            decision_path = run_dir / "control" / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "gate_id": "assembly_review",
                        "review_request_id": persisted.review_request_id,
                        "review_request_hash": persisted.request_hash,
                        "decision": "accept",
                        "candidate_id": "flye",
                    }
                ),
                encoding="utf-8",
            )

            class _ExplodingGraph:
                def __init__(self, real_graph):
                    self._real = real_graph
                    self.checkpointer = real_graph.checkpointer

                def get_state(self, *args, **kwargs):
                    return self._real.get_state(*args, **kwargs)

                def update_state(self, *args, **kwargs):
                    return self._real.update_state(*args, **kwargs)

                def invoke(self, *args, **kwargs):
                    raise RuntimeError("simulated graph failure during resume")

            real_build = build_graph

            def _wrap_build(*args, **kwargs):
                return _ExplodingGraph(real_build(*args, **kwargs))

            with _patch("app.entrypoints.run.build_graph", side_effect=_wrap_build):
                code, _stdout, stderr = _capture(
                    _run_resume,
                    argparse.Namespace(
                        session_id="s1",
                        decision_file=str(decision_path),
                        data_dir=str(data_root),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 1)
            self.assertIn("resume invoke failed", stderr)
            self.assertIn("simulated graph failure", stderr)
            self.assertNotIn("Traceback", stderr)

            ledger_events = Ledger(run_dir).read_events("s1")
            failed = [e for e in ledger_events if e["event_type"] == "resume_failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("simulated graph failure", failed[0]["error"])


if __name__ == "__main__":
    unittest.main()
