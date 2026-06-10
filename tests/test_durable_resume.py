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
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.control.ledger import Ledger
from app.entrypoints.run import _run_resume
from app.graph.builder import build_graph


def _build_blocked_state(data_dir: Path, *, session_id: str = "s1") -> tuple[dict, Path, Path]:
    review_dir = data_dir / "artifacts" / "assembly_review" / session_id
    review_dir.mkdir(parents=True, exist_ok=True)
    request_ref = review_dir / "assembly_review_request.json"
    draft_path = data_dir / "draft.fasta"
    draft_path.write_text(">c\nACGT\n", encoding="utf-8")
    payload = {
        "stage": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {
                "candidate_id": "flye",
                "method_name": "assembly.candidate.flye",
                "final_assembly_ref": str(draft_path),
            }
        ],
        "signals": [],
        "decisions_allowed": ["accept", "stop_human_review"],
        "notes": [],
        "blocked_at": datetime.now(timezone.utc).isoformat(),
        "review_request_artifact_ref": str(request_ref),
    }
    request_ref.write_text(json.dumps(payload), encoding="utf-8")
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "manifest.yaml")
    ledger.record_review_requested(
        session_id,
        "assembly_review",
        reason=payload["reason"],
        candidates=payload["candidates"],
    )
    state = {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": "assembly_review",
        "input_manifest_ref": "manifest.yaml",
        "active_interrupt": payload,
        "stage_outputs": {
            "assembly_review": {
                "status": "blocked_human",
                "candidates": payload["candidates"],
                "rejected_candidates": {},
                "review_request_artifact_ref": str(request_ref),
            }
        },
    }
    return state, request_ref, draft_path


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
    graph = build_graph(backend="sqlite", data_dir=str(data_dir))
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
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            state, _request_ref, draft_path = _build_blocked_state(data_dir)
            _seed_blocked_checkpoint(data_dir, state)

            decision_path = data_dir / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "final_assembly_ref": str(draft_path),
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
                    data_dir=str(data_dir),
                    checkpointer="sqlite",
                ),
            )

            # The graph may exit non-zero if a downstream stage (e.g.
            # function_annotation) hits its own gate; what matters here is
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

            ledger_events = Ledger(data_dir).read_events("s1")
            decision_events = [e for e in ledger_events if e["event_type"] == "review_decision"]
            self.assertTrue(decision_events, "expected review_decision ledger event")
            # The accepted assembly_review decision advances downstream into the
            # annotation chain, which records its own gate decisions; assert on
            # the assembly_review decision specifically rather than the last one.
            assembly_decisions = [e for e in decision_events if e["stage"] == "assembly_review"]
            self.assertTrue(assembly_decisions, "expected assembly_review review_decision ledger event")
            self.assertEqual(assembly_decisions[-1]["decision"], "accept")
            self.assertEqual(assembly_decisions[-1]["candidate_id"], "flye")

            # F3: lifecycle continuation events around the resume.
            resume_started = [e for e in ledger_events if e["event_type"] == "resume_started"]
            resume_completed = [e for e in ledger_events if e["event_type"] == "resume_completed"]
            self.assertEqual(len(resume_started), 1)
            self.assertEqual(len(resume_completed), 1)
            self.assertEqual(resume_started[0]["source"], "cli")
            self.assertEqual(resume_started[0]["decision_file_ref"], str(decision_path))
            self.assertEqual(resume_started[0]["decision"]["stage"], "assembly_review")
            self.assertEqual(resume_started[0]["decision"]["decision"], "accept")
            # N3: resume events should record backend + data_dir so the
            # ledger trail distinguishes between sqlite and memory runs.
            self.assertEqual(resume_started[0]["checkpointer"], "sqlite")
            self.assertEqual(resume_started[0]["data_dir"], str(data_dir))
            self.assertEqual(resume_completed[0]["checkpointer"], "sqlite")
            self.assertIn(resume_completed[0]["supervision_state"], {"running_stage", "completed", "failed_terminal", "blocked_human"})
            self.assertIn("current_stage", resume_completed[0])
            # resume_started must precede review_decision and resume_completed
            event_types = [e["event_type"] for e in ledger_events]
            self.assertLess(event_types.index("resume_started"), event_types.index("review_decision"))
            self.assertLess(event_types.index("review_decision"), event_types.index("resume_completed"))

            graph = build_graph(backend="sqlite", data_dir=str(data_dir))
            snap = graph.get_state({"configurable": {"thread_id": "s1"}})
            advanced_past_assembly_review = snap.values["current_stage"] not in {"assembly_review"}
            self.assertTrue(
                advanced_past_assembly_review,
                f"expected current_stage past assembly_review, got {snap.values['current_stage']!r}",
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
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            state, _request_ref, _draft_path = _build_blocked_state(data_dir)
            _seed_blocked_checkpoint(data_dir, state)

            decision_path = data_dir / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "unknown_candidate",
                        "final_assembly_ref": "missing.fasta",
                    }
                ),
                encoding="utf-8",
            )

            code, _stdout, stderr = _capture(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision_path),
                    data_dir=str(data_dir),
                    checkpointer="sqlite",
                ),
            )
            self.assertEqual(code, 1)
            self.assertIn("resume rejected", stderr)

            graph = build_graph(backend="sqlite", data_dir=str(data_dir))
            snap = graph.get_state({"configurable": {"thread_id": "s1"}})
            self.assertEqual(snap.values["supervision_state"], "blocked_human")
            self.assertEqual(snap.values["current_stage"], "assembly_review")
            self.assertIsNotNone(snap.values["active_interrupt"])
            if hasattr(graph.checkpointer, "conn"):
                graph.checkpointer.conn.close()

            ledger_events = Ledger(data_dir).read_events("s1")
            decision_events = [e for e in ledger_events if e["event_type"] == "review_decision"]
            self.assertEqual(decision_events, [], "no review_decision should be written for an invalid resume")
            # F3: an invalid decision should still emit resume_started +
            # resume_failed so the audit trail records the attempt.
            started = [e for e in ledger_events if e["event_type"] == "resume_started"]
            failed = [e for e in ledger_events if e["event_type"] == "resume_failed"]
            self.assertEqual(len(started), 1)
            self.assertEqual(len(failed), 1)
            self.assertIn("error", failed[0])

    def test_resume_with_no_thread_in_checkpoint_db_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            state, _request_ref, draft_path = _build_blocked_state(data_dir)
            # Seed under a different thread_id so the resume target is missing.
            seeded = dict(state)
            seeded["session_id"] = "other"
            seeded["thread_id"] = "other"
            _seed_blocked_checkpoint(data_dir, seeded)

            decision_path = data_dir / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "final_assembly_ref": str(draft_path),
                    }
                ),
                encoding="utf-8",
            )

            code, _stdout, stderr = _capture(
                _run_resume,
                argparse.Namespace(
                    session_id="s1",
                    decision_file=str(decision_path),
                    data_dir=str(data_dir),
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
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            state, _request_ref, draft_path = _build_blocked_state(data_dir)
            _seed_blocked_checkpoint(data_dir, state)

            decision_path = data_dir / "decision.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "final_assembly_ref": str(draft_path),
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
                        data_dir=str(data_dir),
                        checkpointer="sqlite",
                    ),
                )

            self.assertEqual(code, 1)
            self.assertIn("resume invoke failed", stderr)
            self.assertIn("simulated graph failure", stderr)
            self.assertNotIn("Traceback", stderr)

            ledger_events = Ledger(data_dir).read_events("s1")
            failed = [e for e in ledger_events if e["event_type"] == "resume_failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("simulated graph failure", failed[0]["error"])


if __name__ == "__main__":
    unittest.main()
