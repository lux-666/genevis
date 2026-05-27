"""Interactive CLI for human-gate review and resume.

Provides shared primitives for prompting operator decisions at review gates,
used by both `gene-agent review` (standalone) and the in-process gate loop
in `_run_session`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_blocked_summary(summary: dict[str, Any]) -> None:
    """Print a human-readable summary of a blocked session gate."""
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage", "unknown")
    print(f"\nsession_id:        {summary['session_id']}")
    print(f"supervision_state: {summary['supervision_state']}")
    print(f"blocked_stage:     {stage}")

    if stage == "assembly_review":
        _render_assembly_review(requirements, summary)
    elif stage == "hic_scaffolding":
        _render_hic_scaffolding(requirements, summary)
    else:
        print(f"decisions_allowed: {requirements.get('decisions_allowed', [])}")

    report = summary.get("latest_review_request_artifact")
    if isinstance(report, dict) and report.get("file_path"):
        print(f"review_request:    {report['file_path']}")

    next_action = summary.get("next_action") or {}
    if next_action.get("relevant_refs"):
        print(f"relevant_refs:     {next_action['relevant_refs']}")
    print()


def _render_assembly_review(requirements: dict, summary: dict) -> None:
    decisions = requirements.get("decisions_allowed", [])
    print(f"decisions_allowed: {decisions}")

    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}
    if candidate_ids:
        print("\ncandidates:")
        for cid in candidate_ids:
            ref = candidate_refs.get(cid, "<unknown>")
            exists = Path(ref).exists() if ref != "<unknown>" else False
            marker = "exists" if exists else "MISSING"
            print(f"  [{cid}] {ref}  ({marker})")

    report_refs = requirements.get("report_refs") or {}
    if report_refs:
        print("\nreport_refs:")
        for name, ref in report_refs.items():
            print(f"  {name}: {ref}")

    comparison = requirements.get("candidate_comparison_context") or {}
    if comparison:
        print("\ncandidate_comparison:")
        for key, val in comparison.items():
            print(f"  {key}: {val}")


def _render_hic_scaffolding(requirements: dict, summary: dict) -> None:
    decisions = requirements.get("decisions_allowed", [])
    print(f"decisions_allowed: {decisions}")

    methods = requirements.get("candidate_methods") or []
    if methods:
        print(f"\ncandidate_methods:")
        for m in methods:
            print(f"  - {m}")

    interrupt = (summary.get("latest_review_request") or {}).get("payload", {})
    for key in ("pre_hic_assembly_ref", "final_assembly_for_annotation",
                "scaffold_plan_ref", "hic_review_bundle_ref"):
        val = interrupt.get(key) or requirements.get(key)
        if val:
            print(f"{key}: {val}")


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def prompt_assembly_review_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt the operator for an assembly_review decision. Returns None on cancel."""
    decisions = requirements.get("decisions_allowed", [])
    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}

    decision = _prompt_choice("decision", decisions)
    if decision is None:
        return None

    candidate_id = None
    final_assembly_ref = None
    rejected = []

    if decision in ("accept", "pass", "purge", "polish", "continue_low_confidence", "rerun"):
        if len(candidate_ids) == 1:
            candidate_id = candidate_ids[0]
            final_assembly_ref = candidate_refs.get(candidate_id)
            print(f"  auto-selected candidate: {candidate_id}")
        elif candidate_ids:
            candidate_id = _prompt_choice("candidate", candidate_ids)
            if candidate_id is None:
                return None
            final_assembly_ref = candidate_refs.get(candidate_id)
        else:
            try:
                candidate_id = input("candidate_id: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not candidate_id:
                return None

        if not final_assembly_ref:
            try:
                final_assembly_ref = input(f"final_assembly_ref [{candidate_refs.get(candidate_id, '')}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not final_assembly_ref:
                final_assembly_ref = candidate_refs.get(candidate_id)

        rejected = [c for c in candidate_ids if c != candidate_id]

    try:
        notes_raw = input("notes (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        notes_raw = ""

    return {
        "stage": "assembly_review",
        "decision": decision,
        "candidate_id": candidate_id,
        "final_assembly_ref": final_assembly_ref,
        "rejected_candidates": rejected,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }


def prompt_hic_scaffolding_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt the operator for a hic_scaffolding decision. Returns None on cancel."""
    decisions = requirements.get("decisions_allowed", [])
    methods = requirements.get("candidate_methods") or []

    decision = _prompt_choice("decision", decisions)
    if decision is None:
        return None

    method_name = None
    if decision == "select_method":
        if len(methods) == 1:
            method_name = methods[0]
            print(f"  auto-selected method: {method_name}")
        elif methods:
            method_name = _prompt_choice("method", methods)
            if method_name is None:
                return None
        else:
            try:
                method_name = input("method_name: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not method_name:
                return None

    try:
        notes_raw = input("notes (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        notes_raw = ""

    return {
        "stage": "hic_scaffolding",
        "decision": decision,
        "method_name": method_name,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }


def _prompt_choice(label: str, options: list[str]) -> str | None:
    if not options:
        return None
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        raw = input(f"select {label} [1-{len(options)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        if raw in options:
            return raw
    print(f"invalid selection: {raw!r}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Shared: inspect → prompt → validate → persist
# ---------------------------------------------------------------------------


def collect_gate_decision(
    session_id: str,
    data_dir: str,
) -> tuple[Any, Path] | None:
    """Inspect a blocked session, prompt operator, validate, persist artifact.

    Returns (ResumeReviewRequest, decision_artifact_path) on success,
    or None on cancel/error. Prints errors to stderr.
    """
    from app.control.lifecycle import inspect_session
    from app.review.base import ResumeReviewRequest

    summary = inspect_session(session_id, data_dir)
    if summary.get("error"):
        print(f"error: {summary['error']}", file=sys.stderr)
        return None

    if summary.get("supervision_state") != "blocked_human":
        print(
            f"error: session {session_id!r} is not blocked on a human gate "
            f"(supervision_state={summary.get('supervision_state')!r})",
            file=sys.stderr,
        )
        return None

    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    if stage not in ("assembly_review", "hic_scaffolding"):
        print(
            f"error: unsupported review stage {stage!r}; "
            "only assembly_review and hic_scaffolding are supported",
            file=sys.stderr,
        )
        return None

    render_blocked_summary(summary)

    if stage == "assembly_review":
        raw_decision = prompt_assembly_review_decision(requirements)
    else:
        raw_decision = prompt_hic_scaffolding_decision(requirements)

    if raw_decision is None:
        print("review cancelled")
        return None

    raw_decision["session_id"] = session_id
    try:
        request = ResumeReviewRequest.model_validate(raw_decision)
    except Exception as exc:
        print(f"error: invalid decision: {exc}", file=sys.stderr)
        return None

    decision_path = _persist_decision_artifact(raw_decision, stage, session_id, data_dir)
    print(f"decision_artifact: {decision_path}")
    return request, decision_path


def _persist_decision_artifact(
    raw_decision: dict,
    stage: str,
    session_id: str,
    data_dir: str,
) -> Path:
    """Write decision JSON with a timestamp suffix to avoid overwrites."""
    decision_dir = Path(data_dir) / "artifacts" / stage / session_id
    decision_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    decision_path = decision_dir / f"{stage}_interactive_decision_{ts}.json"
    decision_path.write_text(
        json.dumps(raw_decision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return decision_path


# ---------------------------------------------------------------------------
# Standalone CLI entry: gene-agent review
# ---------------------------------------------------------------------------


def run_interactive_review(
    session_id: str,
    data_dir: str,
    *,
    dry_run: bool = False,
) -> int:
    """Full interactive review flow: inspect → prompt → validate → persist → resume."""
    from app.control.lifecycle import (
        DurableResumeUnavailableError,
        ResumeValidationError,
        resume_review_state,
    )
    from app.graph.checkpointer import checkpoint_db_path

    result = collect_gate_decision(session_id, data_dir)
    if result is None:
        return 1
    request, decision_path = result

    if dry_run:
        print("dry_run: decision validated and written; skipping resume")
        return 0

    db_path = checkpoint_db_path(data_dir)
    if not db_path.exists():
        print(f"error: no checkpoint database at {db_path}", file=sys.stderr)
        return 1

    from app.control.ledger import Ledger
    from app.entrypoints.run import build_graph

    graph = build_graph(backend="sqlite", data_dir=data_dir)
    config = {"configurable": {"thread_id": session_id}}
    snapshot = graph.get_state(config)
    if (
        snapshot is None
        or not snapshot.values
        or snapshot.values.get("session_id") != session_id
    ):
        print(f"error: no checkpoint for session {session_id!r}", file=sys.stderr)
        return 1

    state = snapshot.values
    ledger = Ledger(data_dir)

    resume_event_payload = {
        "source": "interactive_review",
        "decision_artifact_ref": str(decision_path),
        "decision": {
            "stage": request.stage,
            "decision": request.decision,
            "candidate_id": request.candidate_id,
            "method_name": request.method_name,
            "decided_by": request.decided_by,
        },
    }
    ledger.record_event(session_id, "resume_started", resume_event_payload)

    try:
        update = resume_review_state(state, request)
    except (ResumeValidationError, DurableResumeUnavailableError) as exc:
        ledger.record_event(
            session_id, "resume_failed",
            {**resume_event_payload, "error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"error: resume rejected: {exc}", file=sys.stderr)
        return 1

    graph.update_state(config, update)
    try:
        final = graph.invoke(None, config=config)
    except Exception as exc:
        ledger.record_event(
            session_id, "resume_failed",
            {**resume_event_payload, "error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"error: resume invoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    ledger.record_event(session_id, "resume_completed", {
        **resume_event_payload,
        "current_stage": final.get("current_stage"),
        "supervision_state": final.get("supervision_state"),
    })

    print(f"\nsession_id:        {session_id}")
    print(f"current_stage:     {final.get('current_stage', 'unknown')}")
    print(f"supervision_state: {final.get('supervision_state', 'unknown')}")
    print(f"route_trace:       {final.get('route_trace', [])}")

    if final.get("supervision_state") == "failed_terminal":
        print(f"error: {final.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1
    return 0
