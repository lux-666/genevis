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

from app.review.gates import find_review_gate, registered_review_stage_label
from app.storage.paths import locate_session_data_dir


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
        _render_generic_gate(requirements)

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

    candidates = requirements.get("method_candidates") or {}
    if candidates:
        print("\nexecuted_candidates:")
        for method, candidate in candidates.items():
            print(f"  [{method}] {candidate.get('status')}")
            for key in ("scaffold_fasta", "review_bundle_ref", "qc_report_ref"):
                if candidate.get(key):
                    print(f"    {key}: {candidate[key]}")

    interrupt = (summary.get("latest_review_request") or {}).get("payload", {})
    for key in ("pre_hic_assembly_ref", "final_assembly_for_annotation",
                "scaffold_plan_ref", "hic_review_bundle_ref",
                "chromosome_scaffolded_assembly_candidate_ref", "hic_qc_report_ref"):
        val = interrupt.get(key) or requirements.get(key)
        if val:
            print(f"{key}: {val}")


def _render_generic_gate(requirements: dict) -> None:
    decisions = requirements.get("decisions_allowed", [])
    print(f"decisions_allowed: {decisions}")
    candidate_ids = requirements.get("candidate_ids") or []
    if candidate_ids:
        print("\ncandidates:")
        for cid in candidate_ids:
            print(f"  - {cid}")
    methods = requirements.get("candidate_methods") or []
    executed = requirements.get("executed_candidate_methods") or []
    if methods:
        print("\ncandidate_methods:")
        for method in methods:
            print(f"  - {method}")


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
    metadata: dict[str, Any] = {}

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

        if decision not in ("purge", "polish", "rerun"):
            if not final_assembly_ref:
                try:
                    final_assembly_ref = input(f"final_assembly_ref [{candidate_refs.get(candidate_id, '')}]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                if not final_assembly_ref:
                    final_assembly_ref = candidate_refs.get(candidate_id)

        rejected = [c for c in candidate_ids if c != candidate_id]

    if decision in ("purge", "polish"):
        metadata = _prompt_transform_action_metadata(decision)
        if metadata is None:
            return None
        final_assembly_ref = metadata.get("action_output_ref")
    elif decision == "rerun":
        metadata = _prompt_rerun_metadata()
        if metadata is None:
            return None

    try:
        notes_raw = input("notes (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        notes_raw = ""

    result: dict[str, Any] = {
        "stage": "assembly_review",
        "decision": decision,
        "candidate_id": candidate_id,
        "final_assembly_ref": final_assembly_ref,
        "rejected_candidates": rejected,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }
    if metadata:
        result["metadata"] = metadata
    return result


def prompt_hic_scaffolding_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt the operator for a hic_scaffolding decision. Returns None on cancel."""
    decisions = requirements.get("decisions_allowed", [])
    methods = requirements.get("candidate_methods") or []
    executed = requirements.get("executed_candidate_methods") or []

    decision = _prompt_choice("decision", decisions)
    if decision is None:
        return None

    method_name = None
    if decision in {"select_method", "accept_candidate"}:
        choices = executed if decision == "accept_candidate" else methods
        if len(choices) == 1:
            method_name = choices[0]
            print(f"  auto-selected method: {method_name}")
        elif choices:
            method_name = _prompt_choice("method", choices)
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


def prompt_generic_gate_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt for a registered gate without a specialized adapter."""
    stage = requirements.get("stage")
    decisions = requirements.get("decisions_allowed", [])
    candidate_ids = requirements.get("candidate_ids") or []
    methods = requirements.get("candidate_methods") or []

    decision = _prompt_choice("decision", decisions)
    if decision is None:
        return None

    candidate_id = None
    method_name = None
    if candidate_ids and decision not in {"skip", "stop_human_review"}:
        if len(candidate_ids) == 1:
            candidate_id = candidate_ids[0]
            print(f"  auto-selected candidate: {candidate_id}")
        else:
            candidate_id = _prompt_choice("candidate", candidate_ids)
            if candidate_id is None:
                return None
    if methods and decision == "select_method":
        if len(methods) == 1:
            method_name = methods[0]
            print(f"  auto-selected method: {method_name}")
        else:
            method_name = _prompt_choice("method", methods)
            if method_name is None:
                return None

    try:
        notes_raw = input("notes (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        notes_raw = ""

    result: dict[str, Any] = {
        "stage": stage,
        "gate_id": requirements.get("gate_id"),
        "decision": decision,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }
    if candidate_id:
        result["candidate_id"] = candidate_id
        result["rejected_candidates"] = [candidate for candidate in candidate_ids if candidate != candidate_id]
    if method_name:
        result["method_name"] = method_name
    return result


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


def _prompt_transform_action_metadata(decision: str) -> dict[str, Any] | None:
    """Collect metadata required for purge/polish decisions."""
    print(f"\n  {decision} requires the path to the already-completed output assembly.")
    try:
        action_output_ref = input("action_output_ref (path to purged/polished assembly): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not action_output_ref:
        print("error: action_output_ref is required", file=sys.stderr)
        return None

    try:
        method_name = input("method_name (tool used, e.g. purge_dups/nextpolish; required): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not method_name:
        print("error: method_name is required for transform actions", file=sys.stderr)
        return None

    try:
        evidence = input("evidence_notes (what confirms the action, e.g. 'purge_dups log shows 5% removed'): ").strip()
    except (EOFError, KeyboardInterrupt):
        evidence = ""

    if not evidence:
        print("error: evidence_notes is required for transform actions", file=sys.stderr)
        return None

    metadata: dict[str, Any] = {
        "action_output_ref": action_output_ref,
        "human_approved_action": True,
        "evidence_notes": [evidence],
        "method_name": method_name,
    }
    return metadata


def _prompt_rerun_metadata() -> dict[str, Any] | None:
    """Collect metadata required for rerun decisions."""
    print("\n  rerun requires adjusted Flye parameters for the new assembly attempt.")
    try:
        genome_size = input("estimated_genome_size (e.g. 500m, 2g): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not genome_size:
        print("error: estimated_genome_size is required for rerun", file=sys.stderr)
        return None

    try:
        read_type = input("read_type (e.g. nano-raw, nano-hq, pacbio-hifi): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not read_type:
        print("error: read_type is required for rerun", file=sys.stderr)
        return None

    try:
        threads = input("threads [4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        threads = ""
    threads_val = int(threads) if threads.isdigit() else 4

    return {
        "rerun_parameters": {
            "estimated_genome_size": genome_size,
            "read_type": read_type,
            "threads": threads_val,
        },
    }


# ---------------------------------------------------------------------------
# Decision brief: consolidated evidence for human judgment
# ---------------------------------------------------------------------------


def build_decision_brief(
    summary: dict[str, Any],
    data_dir: str,
) -> dict[str, Any]:
    """Build a consolidated decision brief from inspect summary and artifacts.

    Returns a structured dict with sections the TUI can render to help the
    operator make an informed gate decision. Designed as the preparation layer
    for future "integrated results for human judgment" in the TUI.
    """
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage", "unknown")

    brief: dict[str, Any] = {
        "session_id": summary.get("session_id"),
        "stage": stage,
        "supervision_state": summary.get("supervision_state"),
        "decisions_allowed": requirements.get("decisions_allowed", []),
    }

    request_artifact = summary.get("latest_review_request_artifact")
    if isinstance(request_artifact, dict) and request_artifact.get("file_path"):
        brief["review_request_ref"] = request_artifact["file_path"]
    request_payload = _load_artifact_payload(request_artifact)

    if stage == "assembly_review":
        brief["candidates"] = _brief_assembly_candidates(requirements, request_payload)
        brief["evidence"] = _brief_assembly_evidence(requirements, request_payload, data_dir, summary)
    elif stage == "hic_scaffolding":
        brief["candidates"] = _brief_hic_candidates(requirements, request_payload)
        brief["evidence"] = _brief_hic_evidence(requirements, request_payload, summary)
    else:
        brief["candidates"] = _brief_generic_candidates(requirements, request_payload)
        brief["evidence"] = _brief_generic_evidence(requirements, request_payload)

    next_action = summary.get("next_action") or {}
    if next_action.get("relevant_refs"):
        brief["relevant_refs"] = next_action["relevant_refs"]

    return brief


def render_decision_brief(brief: dict[str, Any]) -> None:
    """Render a decision brief to stdout for the operator."""
    print(f"\n{'=' * 50}")
    print("Decision Brief")
    print(f"{'=' * 50}")
    print(f"\nSession:  {brief.get('session_id')}")
    print(f"Stage:    {brief.get('stage')}")
    print(f"State:    {brief.get('supervision_state')}")
    print(f"Allowed:  {', '.join(brief.get('decisions_allowed', []))}")

    if brief.get("review_request_ref"):
        print(f"Request:  {brief['review_request_ref']}")

    candidates = brief.get("candidates") or []
    if candidates:
        print(f"\nCandidates ({len(candidates)}):")
        for c in candidates:
            status = f" [{c['status']}]" if c.get("status") else ""
            exists_marker = ""
            if c.get("ref"):
                exists_marker = " (exists)" if c.get("exists") else " (MISSING)"
            print(f"  - {c['id']}{status}{exists_marker}")
            if c.get("ref"):
                print(f"    ref: {c['ref']}")
            if c.get("method"):
                print(f"    method: {c['method']}")

    evidence = brief.get("evidence") or {}
    if evidence.get("metrics"):
        print("\nMetrics:")
        for key, val in evidence["metrics"].items():
            print(f"  {key}: {val}")
    if evidence.get("report_refs"):
        print("\nReports:")
        for name, ref in evidence["report_refs"].items():
            print(f"  {name}: {ref}")
    if evidence.get("notes"):
        print("\nNotes:")
        for note in evidence["notes"]:
            print(f"  - {note}")

    refs = brief.get("relevant_refs")
    if refs:
        print("\nRelevant refs:")
        for ref in (refs if isinstance(refs, list) else [refs]):
            print(f"  {ref}")

    print(f"{'=' * 50}\n")


def _load_artifact_payload(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not artifact:
        return None
    file_path = artifact.get("file_path")
    if not file_path:
        return None
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _brief_assembly_candidates(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}
    payload_candidates = (request_payload or {}).get("candidates") or []
    payload_map = {c.get("candidate_id"): c for c in payload_candidates if isinstance(c, dict)}

    result = []
    for cid in candidate_ids:
        entry: dict[str, Any] = {"id": cid, "ref": candidate_refs.get(cid)}
        pc = payload_map.get(cid) or {}
        if pc.get("method_name"):
            entry["method"] = pc["method_name"]
        if pc.get("status"):
            entry["status"] = pc["status"]
        ref_path = Path(entry["ref"]) if entry.get("ref") else None
        entry["exists"] = ref_path.exists() if ref_path else False
        result.append(entry)
    return result


def _brief_assembly_evidence(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
    data_dir: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}

    comparison = requirements.get("candidate_comparison_context") or {}
    if comparison:
        evidence["metrics"] = comparison

    report_refs = requirements.get("report_refs") or {}
    if report_refs:
        evidence["report_refs"] = report_refs

    payload = request_payload or {}
    notes = payload.get("notes") or []
    signals = payload.get("signals") or []
    if notes or signals:
        evidence["notes"] = [*notes, *[s if isinstance(s, str) else str(s) for s in signals]]

    return evidence


def _brief_hic_candidates(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    methods = requirements.get("candidate_methods") or []
    payload_candidates = (request_payload or {}).get("candidates") or []
    payload_map = {c.get("method_name"): c for c in payload_candidates if isinstance(c, dict)}

    result = []
    for method in methods:
        entry: dict[str, Any] = {"id": method, "method": method}
        pc = payload_map.get(method) or {}
        if pc.get("status"):
            entry["status"] = pc["status"]
        result.append(entry)
    return result


def _brief_hic_evidence(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
    summary: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}

    payload = request_payload or {}
    notes = payload.get("notes") or []
    if notes:
        evidence["notes"] = notes

    interrupt_payload = (summary.get("latest_review_request") or {}).get("payload", {})
    refs: dict[str, str] = {}
    for key in ("pre_hic_assembly_ref", "scaffold_plan_ref", "hic_review_bundle_ref"):
        val = interrupt_payload.get(key) or requirements.get(key)
        if val:
            refs[key] = val
    if refs:
        evidence["report_refs"] = refs

    return evidence


def _brief_generic_candidates(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    payload_candidates = (request_payload or {}).get("candidates") or []
    candidates = payload_candidates if isinstance(payload_candidates, list) else []
    result: list[dict[str, Any]] = []
    for cid in requirements.get("candidate_ids") or []:
        entry: dict[str, Any] = {"id": cid}
        match = next((c for c in candidates if isinstance(c, dict) and c.get("candidate_id") == cid), {})
        if isinstance(match, dict):
            entry["status"] = match.get("status")
            if match.get("method_name"):
                entry["method"] = match["method_name"]
        result.append(entry)
    for method in requirements.get("candidate_methods") or []:
        if any(candidate.get("method") == method for candidate in result):
            continue
        result.append({"id": method, "method": method})
    return result


def _brief_generic_evidence(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    report_refs = requirements.get("report_refs") or {}
    if report_refs:
        evidence["report_refs"] = report_refs
    notes = (request_payload or {}).get("notes") or []
    if notes:
        evidence["notes"] = notes
    return evidence


# ---------------------------------------------------------------------------
# Shared: inspect → prompt → validate → persist
# ---------------------------------------------------------------------------


def collect_gate_decision(
    session_id: str,
    data_dir: str,
) -> tuple["ResumeReviewRequest", dict[str, Any]] | None:
    """Inspect a blocked session, prompt operator, validate request schema.

    Returns (ResumeReviewRequest, raw_decision_dict) on success,
    or None on cancel/error. Does NOT persist the decision artifact —
    callers should call persist_decision_artifact() after full resume
    validation succeeds.
    """
    from app.control.lifecycle import inspect_session
    from app.review.base import ResumeReviewRequest

    summary = _checkpoint_blocked_summary(session_id, data_dir) or inspect_session(session_id, data_dir)
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
    gate = find_review_gate(stage, requirements.get("gate_id"))
    if gate is None:
        print(
            f"error: unsupported review stage {stage!r}; "
            f"registered review stages: {registered_review_stage_label()}",
            file=sys.stderr,
        )
        return None

    brief = build_decision_brief(summary, data_dir)
    render_decision_brief(brief)

    if gate.prompt_adapter == "assembly_review":
        raw_decision = prompt_assembly_review_decision(requirements)
    elif gate.prompt_adapter == "hic_scaffolding":
        raw_decision = prompt_hic_scaffolding_decision(requirements)
    else:
        raw_decision = prompt_generic_gate_decision(requirements)

    if raw_decision is None:
        print("review cancelled")
        return None

    raw_decision["gate_id"] = requirements.get("gate_id") or gate.gate_id
    raw_decision["session_id"] = session_id
    try:
        request = ResumeReviewRequest.model_validate(raw_decision)
    except Exception as exc:
        print(f"error: invalid decision: {exc}", file=sys.stderr)
        return None

    return request, raw_decision


def _checkpoint_blocked_summary(session_id: str, data_dir: str) -> dict[str, Any] | None:
    """Return a blocked-state summary from sqlite checkpoint state, if present."""
    try:
        from app.control.lifecycle import build_result_next_action, inspect_state
        from app.graph.builder import build_graph
        from app.graph.checkpointer import checkpoint_db_path

        if not checkpoint_db_path(data_dir).exists():
            return None
        graph = build_graph(backend="sqlite", data_dir=data_dir)
        snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    except Exception:
        return None
    if (
        snapshot is None
        or not snapshot.values
        or snapshot.values.get("session_id") != session_id
        or snapshot.values.get("supervision_state") != "blocked_human"
    ):
        return None

    state = snapshot.values
    summary = inspect_state(state)
    summary["checkpoint_persistence"] = "sqlite"
    summary["durable_resume_available"] = True
    summary["latest_review_request_artifact"] = None
    active_interrupt = state.get("active_interrupt") or {}
    request_ref = active_interrupt.get("review_request_artifact_ref")
    if request_ref:
        summary["latest_review_request_artifact"] = {"file_path": request_ref}
    summary["latest_review_request"] = {"payload": active_interrupt}
    summary["next_action"] = build_result_next_action(
        {**state, "session_id": session_id, "data_dir": data_dir, "checkpoint_persistence": "sqlite"}
    )
    return summary


def persist_decision_artifact(
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
    located = locate_session_data_dir(data_dir, session_id)
    data_dir = str(located or Path(data_dir).expanduser().resolve(strict=False))
    from app.control.lifecycle import (
        DurableResumeUnavailableError,
        ResumeValidationError,
        resume_review_state,
    )
    from app.graph.checkpointer import checkpoint_db_path
    from app.review.annotation_intake import collect_annotation_intake_selection, persist_annotation_intake_plan

    result = collect_gate_decision(session_id, data_dir)
    if result is None:
        return 1
    request, raw_decision = result

    if dry_run:
        decision_path = persist_decision_artifact(raw_decision, request.stage, session_id, data_dir)
        print(f"decision_artifact: {decision_path}")
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

    intake_selection = None
    try:
        intake_selection = collect_annotation_intake_selection(
            state=state,
            request=request,
        )
        update = resume_review_state(state, request)
        intake = None
        if intake_selection is not None:
            merged_state = {**state, **update}
            intake = persist_annotation_intake_plan(
                state=merged_state,
                recommended_plan=intake_selection.recommended_plan,
                final_plan=intake_selection.final_plan,
                request=request,
                source="interactive_review",
            )
            update = {**update, **intake.update}
    except (ResumeValidationError, DurableResumeUnavailableError) as exc:
        from app.control.ledger import Ledger as _Ledger
        _Ledger(data_dir).record_event(session_id, "resume_rejected", {
            "source": "interactive_review",
            "error": f"{type(exc).__name__}: {exc}",
            "decision": {"stage": request.stage, "decision": request.decision},
        })
        print(f"error: resume rejected: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        from app.control.ledger import Ledger as _Ledger
        _Ledger(data_dir).record_event(session_id, "resume_rejected", {
            "source": "interactive_review",
            "error": f"{type(exc).__name__}: {exc}",
            "decision": {"stage": request.stage, "decision": request.decision},
        })
        print(f"error: resume rejected: {exc}", file=sys.stderr)
        return 1

    decision_path = persist_decision_artifact(raw_decision, request.stage, session_id, data_dir)
    print(f"decision_artifact: {decision_path}")
    if intake is not None:
        print(f"annotation_intake_plan: {intake.artifact_ref}")

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
