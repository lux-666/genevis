"""Interactive CLI for human-gate review and resume.

Provides shared primitives for prompting operator decisions at review gates,
used by both `gene-agent review` (standalone) and the in-process gate loop
in `_run_session`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.review.gates import find_review_gate, registered_review_stage_label
from app.storage.paths import SessionPaths, locate_session_data_dir

if TYPE_CHECKING:
    from app.review.base import ResumeReviewRequest

_MAX_INVALID_PROMPT_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

ASSEMBLY_REVIEW_DOWNSTREAM_FLOW = (
    "assembly_review -> hic_scaffolding -> t2t -> "
    "repeat_annotation -> gene_annotation -> function_annotation"
)

ASSEMBLY_REVIEW_DECISION_HELP = {
    "accept": (
        "Approve the selected assembly and continue to Hi-C scaffolding, "
        "then T2T, then annotation stages."
    ),
    "pass": (
        "Same as accept: pass the selected assembly downstream to Hi-C scaffolding, "
        "then T2T, then annotation stages."
    ),
    "continue_low_confidence": (
        "Continue downstream despite weak or incomplete evidence; "
        "the next stage is still Hi-C scaffolding."
    ),
    "purge": (
        "Use an already generated purged assembly as the downstream input; "
        "provide its output path and evidence."
    ),
    "polish": (
        "Use an already generated polished assembly as the downstream input; "
        "provide its output path and evidence."
    ),
    "stop_human_review": (
        "Stop here and leave downstream Hi-C/T2T/annotation stages unstarted."
    ),
}

DECISION_LABELS = {
    "accept": "accept assembly",
    "pass": "accept assembly unchanged",
    "continue_low_confidence": "continue with weak evidence",
    "purge": "use purged assembly",
    "polish": "use polished assembly",
    "select_cyclone": "use CycloneSEQ route",
    "select_ont": "use ONT route",
    "select_hifi": "use HiFi route",
    "accept_candidate": "accept scaffold result",
    "keep_prior_assembly": "keep prior contig assembly",
    "request_parameter_retry": "request a new parameter recommendation",
    "select_method": "choose result/method",
    "skip": "skip this stage",
    "stop_human_review": "stop workflow",
    "accept_selected_candidates": "accept T2T patch",
    "continue_without_patch": "continue without T2T patch",
    "stop_pipeline": "stop workflow",
    "defer_annotation": "defer annotation",
    "proceed_merge": "merge gene candidates",
    "merge_with_evm": "merge with EVM",
    "request_rerun": "request rerun",
    "stop": "stop workflow",
    "continue": "continue",
    "approve_parameters": "approve recommended parameters",
    "reject_parameters": "reject recommended parameters",
}

DECISION_HELP = {
    **ASSEMBLY_REVIEW_DECISION_HELP,
    "select_cyclone": "use the CycloneSEQ assembly route.",
    "select_ont": "use the ONT assembly route.",
    "select_hifi": "use the HiFi assembly route.",
    "accept_candidate": (
        "use the reviewed scaffold result and continue to T2T/annotation."
    ),
    "keep_prior_assembly": (
        "reject the AutoHiC result and continue with the prior contig assembly."
    ),
    "request_parameter_retry": (
        "seal this AutoHiC attempt and request a fresh sparse parameter "
        "recommendation."
    ),
    "select_method": "choose one available method/result for this gate.",
    "skip": "do not use this stage's result; continue with the previous assembly.",
    "stop_human_review": "stop the workflow here.",
    "accept_selected_candidates": "use the selected T2T patch candidates.",
    "continue_without_patch": "continue without applying T2T patches.",
    "stop_pipeline": "stop the workflow here.",
    "defer_annotation": "pause before annotation.",
    "proceed_merge": "merge the selected gene annotation candidates.",
    "merge_with_evm": "merge candidates through EVM.",
    "request_rerun": "ask for another run before continuing.",
    "stop": "stop the workflow here.",
    "continue": "continue past this gate.",
    "approve_parameters": (
        "approve this exact, hash-bound parameter recommendation and continue "
        "execution."
    ),
    "reject_parameters": (
        "reject this parameter recommendation and stop the current workflow "
        "attempt."
    ),
}


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
    _render_choices(decisions)
    _render_assembly_review_downstream_context(decisions)

    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}
    if candidate_ids:
        print("\ncandidates:")
        for cid in candidate_ids:
            ref = candidate_refs.get(cid, "<unknown>")
            exists = _looks_like_existing_fasta(ref) if ref != "<unknown>" else False
            marker = "FASTA ok" if exists else "MISSING/invalid FASTA"
            print(f"  [{cid}] {ref}  ({marker})")
            _print_candidate_core_reports(requirements, cid, indent="    ")

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


def _render_assembly_review_downstream_context(decisions: list[str]) -> None:
    print("\nafter_accepting_assembly:")
    print(f"  flow: {ASSEMBLY_REVIEW_DOWNSTREAM_FLOW}")
    print(
        "  note: accepting an assembly continues to hic_scaffolding, then t2t, "
        "before annotation."
    )
    if decisions:
        _render_decision_meanings(decisions)


def _render_hic_scaffolding(requirements: dict, summary: dict) -> None:
    decisions = requirements.get("decisions_allowed", [])
    _render_choices(decisions)
    _render_decision_meanings(decisions)

    methods = requirements.get("candidate_methods") or []
    if methods:
        print("\ncandidate_methods:")
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
    for key in (
        "pre_hic_assembly_ref",
        "output_assembly_ref",
        "hic_review_bundle_ref",
        "chromosome_scaffolded_assembly_candidate_ref",
        "hic_qc_report_ref",
    ):
        val = interrupt.get(key) or requirements.get(key)
        if val:
            print(f"{key}: {val}")


def _render_choices(decisions: list[str]) -> None:
    if not decisions:
        print("choices: <none>")
        return
    print("choices:")
    for decision in decisions:
        print(f"  - {_choice_display(decision)}")


def _render_decision_meanings(decisions: list[str]) -> None:
    meanings = [(decision, DECISION_HELP.get(decision)) for decision in decisions]
    meanings = [(decision, help_text) for decision, help_text in meanings if help_text]
    if not meanings:
        return
    print("\nchoice_help:")
    for decision, help_text in meanings:
        print(f"  {_choice_display(decision)}: {help_text}")


def _render_generic_gate(requirements: dict) -> None:
    decisions = requirements.get("decisions_allowed", [])
    _render_choices(decisions)
    _render_decision_meanings(decisions)
    candidate_ids = requirements.get("candidate_ids") or []
    if candidate_ids:
        print("\ncandidates:")
        for cid in candidate_ids:
            print(f"  - {cid}")
    methods = requirements.get("candidate_methods") or []
    if methods:
        print("\ncandidate_methods:")
        for method in methods:
            print(f"  - {method}")


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def prompt_assembly_review_decision(
    requirements: dict[str, Any],
    *,
    data_dir: str | None = None,
) -> dict[str, Any] | None:
    """Prompt the operator for an assembly_review decision. Returns None on cancel."""
    decisions = requirements.get("decisions_allowed", [])
    if "rerun" in decisions:
        print(
            "error: legacy assembly_review rerun requirements are unsupported; "
            "parameter retries must use the v3 recommendation and approval workflow",
            file=sys.stderr,
        )
        return None
    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}

    decision = _prompt_choice("decision", decisions, DECISION_HELP)
    if decision is None:
        return None

    _print_selected_assembly_review_decision_effect(decision)

    candidate_id = None
    final_assembly_ref = None
    rejected = []
    metadata: dict[str, Any] = {}

    if decision in ("accept", "pass", "purge", "polish", "continue_low_confidence"):
        selectable_candidates = list(candidate_ids)
        if decision in {"accept", "pass", "continue_low_confidence"}:
            selectable_candidates = _materialized_assembly_candidates(
                candidate_ids,
                candidate_refs,
                data_dir=data_dir,
            )
            if len(selectable_candidates) != len(candidate_ids):
                skipped = [
                    cid for cid in candidate_ids if cid not in selectable_candidates
                ]
                print("\nunavailable_candidates:")
                for cid in skipped:
                    ref = candidate_refs.get(cid, "")
                    print(f"  - {cid}: FASTA missing or invalid ({ref})")
            if not selectable_candidates:
                print(
                    "error: no materialized assembly FASTA is available for accept",
                    file=sys.stderr,
                )
                return None
            _render_candidate_selection_evidence(
                requirements,
                selectable_candidates,
                candidate_refs,
                data_dir=data_dir,
            )

        if len(selectable_candidates) == 1:
            candidate_id = selectable_candidates[0]
            final_assembly_ref = _execution_candidate_ref(
                candidate_refs.get(candidate_id),
                data_dir=data_dir,
            )
            print(f"  using candidate: {candidate_id}")
        elif selectable_candidates:
            candidate_id = _prompt_choice(
                "candidate",
                selectable_candidates,
                _candidate_choice_descriptions(
                    selectable_candidates,
                    candidate_refs,
                    data_dir=data_dir,
                ),
            )
            if candidate_id is None:
                return None
            final_assembly_ref = _execution_candidate_ref(
                candidate_refs.get(candidate_id),
                data_dir=data_dir,
            )
        else:
            try:
                candidate_id = input("candidate_id: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not candidate_id:
                return None

        if decision not in ("purge", "polish"):
            if not final_assembly_ref:
                try:
                    final_assembly_ref = input(
                        f"final_assembly_ref [{candidate_refs.get(candidate_id, '')}]: "
                    ).strip()
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
    notes_raw = _prompt_notes()

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


def _materialized_assembly_candidates(
    candidate_ids: list[str],
    candidate_refs: dict[str, Any],
    *,
    data_dir: str | None = None,
) -> list[str]:
    return [
        cid
        for cid in candidate_ids
        if _looks_like_existing_fasta(candidate_refs.get(cid), data_dir=data_dir)
    ]


def _execution_candidate_ref(path: Any, *, data_dir: str | None) -> str | None:
    if not path:
        return None
    ref = Path(str(path))
    if ref.is_absolute() or data_dir is None:
        return str(ref)
    return str(SessionPaths.from_run_dir(data_dir).resolve_ref(ref.as_posix()))


def _looks_like_existing_fasta(path: Any, *, data_dir: str | None = None) -> bool:
    if not path:
        return False
    resolved = _execution_candidate_ref(path, data_dir=data_dir)
    if not resolved:
        return False
    ref = Path(resolved)
    if not ref.is_file():
        return False
    try:
        with ref.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    return stripped.startswith(">")
    except OSError:
        return False
    return False


def _render_candidate_selection_evidence(
    requirements: dict[str, Any],
    candidate_ids: list[str],
    candidate_refs: dict[str, Any],
    *,
    data_dir: str | None = None,
) -> None:
    if not candidate_ids:
        return
    print("\ncandidate_evidence:")
    for cid in candidate_ids:
        ref = candidate_refs.get(cid, "")
        marker = (
            "FASTA ok"
            if _looks_like_existing_fasta(ref, data_dir=data_dir)
            else "MISSING/invalid FASTA"
        )
        print(f"  [{cid}] {marker}")
        if ref:
            print(f"    assembly: {ref}")
        _print_candidate_core_reports(requirements, cid, indent="    ")


def _candidate_choice_descriptions(
    candidate_ids: list[str],
    candidate_refs: dict[str, Any],
    *,
    data_dir: str | None = None,
) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for cid in candidate_ids:
        ref = candidate_refs.get(cid, "")
        marker = (
            "FASTA ok"
            if _looks_like_existing_fasta(ref, data_dir=data_dir)
            else "MISSING/invalid FASTA"
        )
        descriptions[cid] = f"{marker}; {ref}"
    return descriptions


def _print_candidate_core_reports(
    requirements: dict[str, Any], candidate_id: str, *, indent: str = ""
) -> None:
    refs = _candidate_core_report_refs(requirements, candidate_id)
    if not refs:
        return
    print(f"{indent}core_reports:")
    for label, ref in refs:
        print(f"{indent}  {label}: {ref}")


def _candidate_core_report_refs(
    requirements: dict[str, Any], candidate_id: str
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    candidate_reports = requirements.get("candidate_report_refs") or {}
    if isinstance(candidate_reports, dict):
        for ref in candidate_reports.get(candidate_id) or []:
            refs.append(("candidate_metric", str(ref)))

    report_refs = requirements.get("report_refs") or {}
    if isinstance(report_refs, dict):
        for key in (
            "assembly_review_report",
            "candidate_comparison",
            "review_packet",
        ):
            if report_refs.get(key):
                refs.append((key, str(report_refs[key])))

    comparison = requirements.get("candidate_comparison_context") or {}
    if isinstance(comparison, dict):
        for key in ("candidate_comparison_ref",):
            if comparison.get(key):
                refs.append((key, str(comparison[key])))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in refs:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _print_selected_assembly_review_decision_effect(decision: str) -> None:
    help_text = DECISION_HELP.get(decision)
    if not help_text:
        return
    print(f"\neffect: {help_text}")
    if decision in {"accept", "pass", "continue_low_confidence", "purge", "polish"}:
        print(f"next: {ASSEMBLY_REVIEW_DOWNSTREAM_FLOW}")


def prompt_hic_scaffolding_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt the operator for a hic_scaffolding decision. Returns None on cancel."""
    decisions = requirements.get("decisions_allowed", [])
    decision = _prompt_choice("decision", decisions, DECISION_HELP)
    if decision is None:
        return None

    notes_raw = _prompt_notes()

    return {
        "stage": "hic_scaffolding",
        "decision": decision,
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

    decision = _prompt_choice("decision", decisions, DECISION_HELP)
    if decision is None:
        return None

    candidate_id = None
    method_name = None
    if candidate_ids and decision not in {"skip", "stop_human_review"}:
        if len(candidate_ids) == 1:
            candidate_id = candidate_ids[0]
            print(f"  using candidate: {candidate_id}")
        else:
            candidate_id = _prompt_choice("candidate", candidate_ids)
            if candidate_id is None:
                return None
    if methods and decision == "select_method":
        if len(methods) == 1:
            method_name = methods[0]
            print(f"  using method: {method_name}")
        else:
            method_name = _prompt_choice("method", methods)
            if method_name is None:
                return None

    notes_raw = _prompt_notes()

    result: dict[str, Any] = {
        "stage": stage,
        "gate_id": requirements.get("gate_id"),
        "decision": decision,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }
    resume_metadata = requirements.get("resume_metadata")
    if isinstance(resume_metadata, dict) and resume_metadata:
        result["metadata"] = dict(resume_metadata)
    if candidate_id:
        result["candidate_id"] = candidate_id
        result["rejected_candidates"] = [
            candidate for candidate in candidate_ids if candidate != candidate_id
        ]
    if method_name:
        result["method_name"] = method_name
    return result


def prompt_t2t_review_decision(
    requirements: dict[str, Any],
) -> dict[str, Any] | None:
    """Prompt for a T2T candidate-selection decision."""
    decisions = requirements.get("decisions_allowed", [])
    candidate_ids = requirements.get("candidate_ids") or []

    decision = _prompt_choice("decision", decisions, DECISION_HELP)
    if decision is None:
        return None

    selected_candidate_ids: list[str] = []
    if decision == "accept_selected_candidates":
        if not candidate_ids:
            print("error: no T2T candidates are available to accept", file=sys.stderr)
            return None
        print("\ncandidates:")
        for index, candidate_id in enumerate(candidate_ids, 1):
            print(f"  {index}. {candidate_id}")
        try:
            raw = input(
                "select candidate(s) [comma-separated numbers or ids]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        selected_candidate_ids = _parse_multi_choice(raw, candidate_ids)
        if not selected_candidate_ids:
            print(f"invalid candidate selection: {raw!r}", file=sys.stderr)
            return None

    notes_raw = _prompt_notes()

    result: dict[str, Any] = {
        "stage": "t2t",
        "gate_id": requirements.get("gate_id"),
        "decision": decision,
        "notes": [notes_raw] if notes_raw else ["interactive review"],
        "decided_by": "human",
    }
    if selected_candidate_ids:
        result["candidate_ids"] = selected_candidate_ids
    return result


def _prompt_choice(
    label: str, options: list[str], descriptions: dict[str, str] | None = None
) -> str | None:
    if not options:
        return None
    display_label = "action" if label == "decision" else label
    print(f"\n{display_label}:")
    for i, opt in enumerate(options, 1):
        suffix = (
            f" - {descriptions[opt]}" if descriptions and descriptions.get(opt) else ""
        )
        print(f"  {i}. {_choice_display(opt)}{suffix}")
    invalid_attempts = 0
    while invalid_attempts < _MAX_INVALID_PROMPT_ATTEMPTS:
        try:
            raw = input(f"select {display_label} [1-{len(options)}]: ").strip()
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
        invalid_attempts += 1
        print(f"invalid selection: {raw!r}", file=sys.stderr)
    print("too many invalid selections; aborting review prompt", file=sys.stderr)
    return None


def _choice_display(option: str) -> str:
    label = DECISION_LABELS.get(option)
    if label and label != option:
        return f"{label} [{option}]"
    return option


def _prompt_notes() -> str:
    try:
        return input("operator note (optional, audit only): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _parse_multi_choice(raw: str, options: list[str]) -> list[str]:
    selections: list[str] = []
    for token in [part.strip() for part in raw.split(",") if part.strip()]:
        choice: str | None = None
        try:
            index = int(token) - 1
        except ValueError:
            if token in options:
                choice = token
        else:
            if 0 <= index < len(options):
                choice = options[index]
        if choice is None:
            return []
        if choice not in selections:
            selections.append(choice)
    return selections


def _prompt_transform_action_metadata(decision: str) -> dict[str, Any] | None:
    """Collect metadata required for purge/polish decisions."""
    print(f"\n  {decision} requires the path to the already-completed output assembly.")
    try:
        action_output_ref = input(
            "action_output_ref (path to purged/polished assembly): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not action_output_ref:
        print("error: action_output_ref is required", file=sys.stderr)
        return None

    try:
        method_name = input(
            "method_name (tool used, e.g. purge_dups/nextpolish; required): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not method_name:
        print("error: method_name is required for transform actions", file=sys.stderr)
        return None

    try:
        evidence = input(
            "evidence_notes (what confirms the action, e.g. "
            "'purge_dups log shows 5% removed'): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        evidence = ""

    if not evidence:
        print(
            "error: evidence_notes is required for transform actions", file=sys.stderr
        )
        return None

    metadata: dict[str, Any] = {
        "action_output_ref": action_output_ref,
        "human_approved_action": True,
        "evidence_notes": [evidence],
        "method_name": method_name,
    }
    return metadata


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
        "runtime_proposal": dict(requirements.get("runtime_proposal") or {}),
    }

    request_artifact = summary.get("latest_review_request_artifact")
    if isinstance(request_artifact, dict) and request_artifact.get("file_path"):
        brief["review_request_ref"] = request_artifact["file_path"]
    request_payload = _load_artifact_payload(request_artifact)

    if requirements.get("gate_id") == "parameter_approval":
        brief["parameter_proposal"] = _brief_parameter_approval(
            requirements,
            request_payload,
            data_dir,
        )
        brief["candidates"] = []
        brief["evidence"] = {}
    elif stage == "assembly_review":
        brief["candidates"] = _brief_assembly_candidates(
            requirements,
            request_payload,
            data_dir=data_dir,
        )
        brief["evidence"] = _brief_assembly_evidence(
            requirements, request_payload, data_dir, summary
        )
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
    choices = ", ".join(
        _choice_display(decision)
        for decision in brief.get("decisions_allowed", [])
    )
    print(f"Choices:  {choices}")

    if brief.get("review_request_ref"):
        print(f"Request:  {brief['review_request_ref']}")

    runtime_proposal = brief.get("runtime_proposal") or {}
    if runtime_proposal:
        print("\nRuntime proposal:")
        print(f"  scope:    {runtime_proposal.get('label')}")
        print(f"  kind:     {runtime_proposal.get('kind')}")
        print(
            "  parameter recommendation: "
            f"{runtime_proposal.get('parameter_recommendation_status')}"
        )
        related_gate = runtime_proposal.get("related_parameter_gate") or {}
        if related_gate:
            print(
                f"  related parameter gate: {related_gate.get('stage')}/"
                f"{related_gate.get('gate_id')}"
            )
        for note in runtime_proposal.get("parameter_recommendation_notes") or []:
            print(f"  note: {note}")
        if runtime_proposal.get("human_confirmation_required"):
            print("  confirmation: human decision required before continuing")

    proposal = brief.get("parameter_proposal") or {}
    if proposal:
        print("\nParameter proposal:")
        print(f"  attempt:  {proposal.get('attempt_id')} ({proposal.get('task')})")
        print(f"  previous: {proposal.get('previous_attempt_id')}")
        print(f"  hash:     {proposal.get('recommendation_hash')}")
        if proposal.get("rationale"):
            print(f"  rationale: {proposal['rationale']}")
        if proposal.get("parameter_diff"):
            print("  changes:")
            for name, change in proposal["parameter_diff"].items():
                print(f"    {name}: {change}")
        if proposal.get("failure_facts"):
            print(f"  failure_facts: {proposal['failure_facts']}")
        if proposal.get("evidence_refs"):
            print("  evidence_refs:")
            for name, ref in proposal["evidence_refs"].items():
                print(f"    {name}: {ref}")
        cases = proposal.get("literature_cases") or []
        if cases:
            print(f"  production paper evidence ({len(cases)}, top-5 maximum):")
            for case in cases:
                doi = f" doi={case['doi']}" if case.get("doi") else ""
                print(f"    - {case.get('case_id')}{doi}")
                for excerpt in case.get("source_excerpts") or []:
                    span_id = excerpt.get("span_id") or "span"
                    print(f"      {span_id}: {excerpt.get('quote')}")

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
            if c.get("reports"):
                print("    core_reports:")
                for label, ref in c["reports"]:
                    print(f"      {label}: {ref}")

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
        for ref in refs if isinstance(refs, list) else [refs]:
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


def _brief_parameter_approval(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
    data_dir: str,
) -> dict[str, Any]:
    """Build the minimum informed-consent view for a parameter approval gate."""

    source = {**(request_payload or {}), **requirements}
    proposal = dict(source.get("resume_metadata") or {})
    for key in (
        "recommendation_manifest_ref",
        "recommendation_ref",
        "parameter_diff",
        "rationale",
        "failure_facts",
        "evidence_refs",
    ):
        if source.get(key) is not None:
            proposal[key] = source.get(key)

    recommendation = _load_ref_payload(source.get("recommendation_ref"), data_dir)
    parsed = (
        recommendation.get("recommendation")
        if isinstance(recommendation, dict)
        else None
    )
    if isinstance(parsed, dict):
        proposal["recommended_params"] = dict(parsed.get("parameters") or {})

    manifest = _load_ref_payload(source.get("recommendation_manifest_ref"), data_dir)
    manifest_refs = manifest.get("refs") if isinstance(manifest, dict) else None
    basis = _load_ref_payload(
        manifest_refs.get("basis_snapshot_ref")
        if isinstance(manifest_refs, dict)
        else None,
        data_dir,
    )
    if isinstance(basis, dict):
        proposal["literature_cases"] = [
            {
                "case_id": case.get("case_id"),
                "doi": (case.get("source") or {}).get("doi"),
                "source_excerpts": list(case.get("source_excerpts") or []),
            }
            for case in (basis.get("literature_cases") or [])[:5]
            if isinstance(case, dict)
        ]
    return proposal


def _load_ref_payload(ref: Any, data_dir: str) -> dict[str, Any] | None:
    expected_sha256 = ref.get("sha256") if isinstance(ref, dict) else None
    if isinstance(ref, dict):
        raw_path = ref.get("file_path") or ref.get("path_ref") or ref.get("path")
    else:
        raw_path = ref
    if not raw_path:
        return None
    path = Path(str(raw_path))
    candidates = [path]
    if not path.is_absolute():
        root = Path(data_dir)
        candidates.extend(
            (root / path, root / str(path).removeprefix("data/"), root.parent / path)
        )
    resolved = next((candidate for candidate in candidates if candidate.exists()), None)
    if resolved is None:
        return None
    try:
        raw = resolved.read_bytes()
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _brief_assembly_candidates(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
    *,
    data_dir: str | None = None,
) -> list[dict[str, Any]]:
    candidate_ids = requirements.get("candidate_ids") or []
    candidate_refs = requirements.get("candidate_final_assembly_refs") or {}
    payload_candidates = (request_payload or {}).get("candidates") or []
    payload_map = {
        c.get("candidate_id"): c for c in payload_candidates if isinstance(c, dict)
    }

    result = []
    for cid in candidate_ids:
        entry: dict[str, Any] = {"id": cid, "ref": candidate_refs.get(cid)}
        pc = payload_map.get(cid) or {}
        if pc.get("method_name"):
            entry["method"] = pc["method_name"]
        if pc.get("status"):
            entry["status"] = pc["status"]
        ref_path = Path(entry["ref"]) if entry.get("ref") else None
        entry["exists"] = (
            _looks_like_existing_fasta(ref_path, data_dir=data_dir)
            if ref_path
            else False
        )
        reports = _candidate_core_report_refs(requirements, cid)
        if reports:
            entry["reports"] = reports
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
        evidence["notes"] = [
            *notes,
            *[s if isinstance(s, str) else str(s) for s in signals],
        ]

    return evidence


def _brief_hic_candidates(
    requirements: dict[str, Any],
    request_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    methods = requirements.get("candidate_methods") or []
    payload_candidates = (request_payload or {}).get("candidates") or []
    payload_map = {
        c.get("method_name"): c for c in payload_candidates if isinstance(c, dict)
    }

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
    for key in ("pre_hic_assembly_ref", "hic_review_bundle_ref"):
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
        match = next(
            (
                c
                for c in candidates
                if isinstance(c, dict) and c.get("candidate_id") == cid
            ),
            {},
        )
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
    or None on cancel/error. The registered gate applier persists the sole
    authoritative decision only after full resume validation succeeds.
    """
    from app.control.lifecycle import inspect_session
    from app.review.base import ResumeReviewRequest

    summary = _checkpoint_blocked_summary(session_id, data_dir) or inspect_session(
        session_id, data_dir
    )
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
        raw_decision = prompt_assembly_review_decision(requirements, data_dir=data_dir)
    elif gate.prompt_adapter == "hic_scaffolding":
        raw_decision = prompt_hic_scaffolding_decision(requirements)
    elif gate.prompt_adapter == "t2t":
        raw_decision = prompt_t2t_review_decision(requirements)
    else:
        raw_decision = prompt_generic_gate_decision(requirements)

    if raw_decision is None:
        print("review cancelled")
        return None

    raw_decision["gate_id"] = requirements.get("gate_id") or gate.gate_id
    raw_decision["session_id"] = session_id
    if requirements.get("review_request_id"):
        raw_decision["review_request_id"] = requirements["review_request_id"]
    request_hash = requirements.get("review_request_hash") or requirements.get(
        "request_hash"
    )
    if request_hash:
        raw_decision["review_request_hash"] = request_hash
    try:
        request = ResumeReviewRequest.model_validate(raw_decision)
    except Exception as exc:
        print(f"error: invalid decision: {exc}", file=sys.stderr)
        return None

    return request, raw_decision


def _checkpoint_blocked_summary(
    session_id: str, data_dir: str
) -> dict[str, Any] | None:
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
        {
            **state,
            "session_id": session_id,
            "data_dir": data_dir,
            "checkpoint_persistence": "sqlite",
        }
    )
    return summary


def persist_decision_artifact(
    raw_decision: dict,
    stage: str,
    session_id: str,
    data_dir: str,
) -> Path:
    """Write a non-authoritative dry-run preview below the run work tree.

    Applied decisions are persisted exactly once by the registered gate under
    ``artifacts/reviews/<review_request_id>/decision.json``.  This helper is
    retained for ``--dry-run`` only and deliberately does not register an
    artifact or create a second source of truth.
    """

    review_request_id = raw_decision.get("review_request_id")
    if not review_request_id:
        raise ValueError("decision preview requires review_request_id")
    decision_dir = SessionPaths.from_run_dir(data_dir).work_path(
        "reviews", review_request_id
    )
    decision_dir.mkdir(parents=True, exist_ok=True)
    decision_path = decision_dir / "decision.preview.json"
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
    data_dir = str(located or SessionPaths(data_dir, session_id).run_dir)
    from app.control.resume_transaction import (
        ResumeTransactionError,
        execute_resume_transaction,
    )
    from app.graph.checkpointer import checkpoint_db_path
    from app.review.annotation_intake import collect_annotation_intake_selection

    result = collect_gate_decision(session_id, data_dir)
    if result is None:
        return 1
    request, raw_decision = result

    if dry_run:
        decision_path = persist_decision_artifact(
            raw_decision, request.stage, session_id, data_dir
        )
        print(f"decision_artifact: {decision_path}")
        print("dry_run: decision validated and written; skipping resume")
        return 0

    db_path = checkpoint_db_path(data_dir)
    if not db_path.exists():
        print(f"error: no checkpoint database at {db_path}", file=sys.stderr)
        return 1

    from app.entrypoints.run import build_graph

    graph = build_graph(backend="sqlite", data_dir=data_dir)
    config = {"configurable": {"thread_id": session_id}}

    try:
        transaction = execute_resume_transaction(
            graph=graph,
            config=config,
            session_id=session_id,
            data_dir=data_dir,
            request=request,
            source="interactive_review",
            checkpointer="sqlite",
            annotation_intake_collector=collect_annotation_intake_selection,
        )
    except ResumeTransactionError as exc:
        if exc.phase == "validation":
            print(f"error: resume rejected: {exc}", file=sys.stderr)
        else:
            print(f"error: resume {exc.phase} failed: {exc}", file=sys.stderr)
        return 1

    final = transaction.state
    if transaction.decision_artifact_path is not None:
        print(f"decision_artifact: {transaction.decision_artifact_path}")
    if transaction.annotation_intake_artifact_ref is not None:
        print(f"annotation_intake_plan: {transaction.annotation_intake_artifact_ref}")

    print(f"\nsession_id:        {session_id}")
    print(f"current_stage:     {final.get('current_stage', 'unknown')}")
    print(f"supervision_state: {final.get('supervision_state', 'unknown')}")
    print(f"route_trace:       {final.get('route_trace', [])}")

    if final.get("supervision_state") == "failed_terminal":
        print(f"error: {final.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1
    return 0
