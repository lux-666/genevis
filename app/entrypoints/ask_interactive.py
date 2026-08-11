"""Interactive planning helpers shared by the TUI."""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict, cast

from pydantic import ValidationError

from app.domains.assembly.selection import (
    FLYE_TO_SHARED_READ_TYPE,
    PLATFORM_TO_SHARED_READ_TYPE,
    SHARED_TO_METHOD_READ_TYPE,
)
from app.entrypoints.ask_planner import (
    _STAGES_NEEDING_METHOD_SELECTION,
    READ_QC_FILTER_METHODS,
    READ_QC_FILTER_SKIP,
    READ_QC_STATS_METHOD,
    _all_selectable_stages,
    _auto_fill_inferred_defaults,
    _default_methods_for_stage,
    _executable_methods_for_stage,
    _fill_draft_default_methods,
    build_run_plan_summary,
    merge_clarification_response,
    next_clarification,
)
from app.schemas.ask_plan import (
    AskClarificationRequest,
    AskClarificationResponse,
    AskPlanState,
    PlanStageOccurrence,
)
from app.schemas.run_config import RequestableStage
from app.schemas.shared_parameters import (
    SharedParameters,
)

try:
    import questionary

    _HAS_QUESTIONARY = True
except ImportError:
    _HAS_QUESTIONARY = False


logger = logging.getLogger(__name__)
_MAX_INVALID_PROMPT_ATTEMPTS = 3
_FIXED_METHOD_STAGES = {"assembly_review", "hic_scaffolding"}


def _fixed_pipeline_description(stage: str) -> str:
    if stage == "assembly_review":
        return "the fixed QUAST and BUSCO pipeline"
    return "the fixed AutoHiC pipeline"


class _UserAbort(Exception):
    pass


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "executable": "run",
    "dependency": "dep",
}

_PARAM_HINTS: dict[str, str] = {
    "assembly_review.eval.busco.lineage_path": (
        "path to BUSCO lineage dir, e.g. /db/busco/insecta_odb10"
    ),
    "assembly_review.eval.busco.lineage_name": "auto-derived from path",
    "assembly_review.eval.busco.lineage_version": "e.g. odb10; auto-detected from name",
    "assembly_review.eval.busco.lineage_source": "local / busco_downloads",
    "assembly_review.eval.busco.threads": "CPU threads for BUSCO",
    "assembly_review.eval.quast.threads": "CPU threads for QUAST",
    "assembly.candidate.flye.threads": "CPU threads for Flye assembler",
    "estimated_genome_size": "e.g. 500m, 2.5g",
    "platform": "sequencing platform for raw long-read registration",
    "read_type": "sequencing platform read type",
    "genome_survey.kmer_count.jellyfish.kmer_size": "k-mer length, typically 21",
    "genome_survey.kmer_count.jellyfish.hash_size": "hash table size, e.g. 1G",
    "genome_survey.kmer_count.jellyfish.threads": "CPU threads for jellyfish",
    "read_qc.length_filter.seqkit_seq.min_len": "minimum read length to keep (bp)",
    "t2t.alignment.minimap2.mode": "auto_patch / manual_review / propose_only",
}

_GENOME_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.IGNORECASE)
_GENOME_SIZE_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
_SHARED_PLATFORM_VALUES = (
    "cycloneseq",
    "ont",
    "clr",
    "pacbio_hifi",
    "illumina",
    "unknown",
)
_SHARED_READ_TYPE_VALUES = tuple(SHARED_TO_METHOD_READ_TYPE.keys())

_STAGE_DESCRIPTIONS: dict[str, str] = {
    "read_qc": "quality stats and optional length filtering",
    "genome_survey": "k-mer profiling and genome size estimation",
    "assembly": "de novo genome assembly",
    "assembly_dedup": "haplotig and overlap removal with purge_dups",
    "assembly_review": "QUAST/BUSCO quality evaluation",
    "hic_scaffolding": "Hi-C based chromosome scaffolding",
}


def _render_confirm(req: AskClarificationRequest) -> None:
    _render_plan_summary(req.summary or {}, title="Execution Plan")


def _review_plan_draft(draft: AskPlanState) -> AskPlanState | None:
    """Review and edit an inferred draft until the operator locks it or restarts."""
    plan = draft
    while True:
        summary = build_run_plan_summary(plan)
        _render_draft_review(plan, summary)
        action = _prompt_draft_action(plan)
        if action == "continue":
            blocking_items = _draft_blocking_items(plan, summary)
            if blocking_items:
                _render_draft_blockers(blocking_items)
                continue
            print(
                "\nDraft locked. Next, review the final run plan before "
                "execution starts."
            )
            return plan
        if action == "restart":
            return None
        if action == "stages":
            plan = _edit_draft_stages(plan)
            continue
        if action.startswith("stage:"):
            _, raw_index = action.split(":", 1)
            plan = _edit_draft_stage(plan, int(raw_index))
            continue
        if action.startswith("method:"):
            _, stage = action.split(":", 1)
            if stage in _FIXED_METHOD_STAGES:
                print(
                    f"{stage} uses {_fixed_pipeline_description(stage)}; "
                    "there is no method choice."
                )
                continue
            plan = _edit_stage_methods(plan, stage)
            continue
        if action.startswith("params:"):
            _, stage = action.split(":", 1)
            plan = _edit_stage_parameters(plan, stage)
            continue
        print(
            "Unknown action. Press Enter to continue, a stage number to edit, "
            "s for stages, or r to restart."
        )


def _render_draft_review(plan: AskPlanState, summary: dict[str, Any]) -> None:
    print()
    print("=" * 64)
    print("Plan Draft")
    print("=" * 64)
    print(f"\nSession:  {summary.get('session_id', '?')}")
    print(f"Data dir: {summary.get('data_dir', '?')}")
    print(f"Manifest: {summary.get('manifest_ref', '?')}")
    _render_summary_inputs(summary)

    print("\nIf you continue:")
    print("  - This draft becomes the working plan.")
    print("  - Execution will NOT start yet.")
    print("  - Missing fields shown below must be fixed here first.")
    print("  - An execution-route plan will be shown before execution.")
    print(
        "  - Runtime-derived proposals are deferred until their prerequisite "
        "results exist."
    )
    print(
        "  - Each deferred proposal will return here as a human gate before "
        "the action runs."
    )
    print("  - You can edit a stage here first, then return to this plan.")

    attention_items = _draft_attention_items(plan, summary)
    order = summary.get("selected_stage_names") or []
    if order:
        print("\nStages:")
        for idx, stage in enumerate(order, start=1):
            _render_draft_stage_summary(
                idx,
                stage,
                plan,
                summary,
                _draft_stage_attention(stage, plan, summary, attention_items),
            )
    else:
        print("\nStages: none selected")

    stages_needing_attention = [
        stage
        for stage in order
        if _draft_stage_attention(stage, plan, summary, attention_items)
    ]
    if stages_needing_attention:
        print("\nNeeds attention:")
        for stage in stages_needing_attention:
            print(f"  - {stage}")

    print("\nActions:")
    print("  Enter / c   continue")
    for idx, stage in enumerate(order, start=1):
        print(f"  {idx:<10} edit {stage}")
    print("  s           edit stage list")
    print("  r           discard and regenerate draft")
    print("=" * 64)


def _render_summary_inputs(summary: dict[str, Any]) -> None:
    """Render input assets. Shared by draft review and final plan summary."""
    input_assets = summary.get("input_assets") or []
    source_paths = summary.get("source_paths")
    if input_assets:
        print("\nInputs:")
        for asset in input_assets:
            paths = list(asset.get("paths") or [])
            label_parts = [str(asset.get("asset_type") or "input")]
            platform = asset.get("platform")
            fmt = asset.get("format")
            details = [
                str(value) for value in (platform, fmt) if value and value != "unknown"
            ]
            if details:
                label_parts.append(f"[{'/'.join(details)}]")
            label = " ".join(label_parts)
            if len(paths) == 1:
                print(f"  {label}: {paths[0]}")
            else:
                print(f"  {label}:")
                for path in paths:
                    print(f"    - {path}")
    elif source_paths:
        print(f"Input:    {', '.join(source_paths)}")


def _render_draft_stage_summary(
    idx: int,
    stage: str,
    plan: AskPlanState,
    summary: dict[str, Any],
    attention: list[str],
    *,
    nested: bool = False,
    occurrence_index: int | None = None,
) -> None:
    print(f"\n{idx}. {stage}")
    occurrence_index = idx - 1 if occurrence_index is None else occurrence_index
    occurrence = (plan.selected_stage_occurrences or [])[occurrence_index]
    print(
        "   local execution: "
        + ("authorized" if occurrence.allow_local_execution else "not authorized")
    )
    print(
        f"   status: {_draft_stage_status(stage, plan, summary, occurrence_index)}"
    )
    method_lines = _draft_stage_method_lines(stage, plan, summary)
    if method_lines:
        print("   methods:")
        for line in method_lines:
            print(f"     {line}")
    parameter_lines = _draft_stage_parameter_lines(stage, plan, summary)
    if parameter_lines:
        heading = (
            "planning/static parameters:"
            if _runtime_points_for_stage(summary, stage)
            else "parameters:"
        )
        print(f"   {heading}")
        for line in parameter_lines:
            print(f"     {line}")
    decision_points = _runtime_points_for_stage(summary, stage)
    runtime_parameter_contracts = _runtime_parameter_contracts_for_stage(summary, stage)
    for contract in runtime_parameter_contracts:
        names = list(contract.get("parameter_names") or [])
        preview_names = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        print(
            "   runtime-derived parameters "
            f"[{contract.get('method', '').rsplit('.', 1)[-1]}]: deferred"
        )
        if preview_names:
            print(f"     catalog L2: {preview_names}")
        constraints = contract.get("user_constraints") or {}
        if constraints:
            print(f"     user constraints (not frozen values): {constraints}")
    if decision_points:
        print("   runtime decisions:")
        for point in decision_points:
            print(f"     {point.get('label')}: deferred")
            print(f"       trigger: {point.get('trigger')}")
    if attention:
        print("   attention:")
        for item in attention:
            print(f"     - {item}")
    if nested and stage in _FIXED_METHOD_STAGES:
        print("   edit: l local execution / p parameters / b back")
    elif nested:
        print("   edit: l local execution / m method / p parameters / b back")
    elif stage in _FIXED_METHOD_STAGES:
        print(f"   edit: {idx} / {idx}p parameters")
    else:
        print(f"   edit: {idx} / {idx}m method / {idx}p parameters")


def _draft_stage_status(
    stage: str,
    plan: AskPlanState,
    summary: dict[str, Any],
    occurrence_index: int,
) -> str:
    occurrence = (plan.selected_stage_occurrences or [])[occurrence_index]
    if not occurrence.allow_local_execution:
        return "included without local execution"
    selected = _selected_methods_for_stage(plan, stage)
    if stage in _FIXED_METHOD_STAGES:
        return f"will run {_fixed_pipeline_description(stage)}"
    if stage in _STAGES_NEEDING_METHOD_SELECTION and not selected:
        return "method needed before locking"
    if selected:
        return "will run locally"
    return "included"


def _draft_stage_method_lines(
    stage: str, plan: AskPlanState, summary: dict[str, Any]
) -> list[str]:
    if stage in _FIXED_METHOD_STAGES:
        return []
    selected = _selected_methods_for_stage(plan, stage)
    if not selected and stage in _STAGES_NEEDING_METHOD_SELECTION:
        return ["missing"]
    return list(selected)


def _draft_stage_parameter_lines(
    stage: str, plan: AskPlanState, summary: dict[str, Any]
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    missing = set(summary.get("missing_parameter_fields") or [])
    shared_stage = _shared_parameter_stage(plan)
    if stage == shared_stage:
        shared_values: dict[str, Any] = {}
        shared_values.update(summary.get("shared_parameter_defaults") or {})
        shared_values.update(summary.get("shared_parameters") or {})
        shared_sources = summary.get("shared_parameter_default_sources") or {}
        for key, value in shared_values.items():
            field = f"shared_parameters.{key}"
            seen.add(field)
            note = _source_note(shared_sources.get(key), fallback="recommended")
            lines.append(f"{key}: {value}{note}")
        for field in sorted(
            item for item in missing if item.startswith("shared_parameters.")
        ):
            seen.add(field)
            lines.append(f"{field.rsplit('.', 1)[-1]}: missing")

    defaults = summary.get("parameter_defaults") or {}
    sources = summary.get("parameter_default_sources") or {}
    params = summary.get("method_parameters") or {}
    for method in _selected_methods_for_stage(plan, stage):
        values = dict(defaults.get(method) or {})
        values.update(params.get(method) or {})
        method_sources = sources.get(method) or {}
        short = method.rsplit(".", 1)[-1]
        deferred_names = _deferred_parameter_names(summary, method)
        for key, value in values.items():
            if key in deferred_names:
                continue
            field = f"{method}.{key}"
            seen.add(field)
            note = (
                _source_note(method_sources.get(key), fallback="recommended")
                if key in (defaults.get(method) or {})
                else ""
            )
            lines.append(f"[{short}] {key}: {value}{note}")
        for field in sorted(item for item in missing if item.startswith(f"{method}.")):
            if field not in seen:
                seen.add(field)
                lines.append(f"[{short}] {field.rsplit('.', 1)[-1]}: missing")

    stage_values: dict[str, Any] = {}
    stage_values.update(
        (summary.get("stage_parameter_defaults") or {}).get(stage) or {}
    )
    stage_values.update((summary.get("stage_parameters") or {}).get(stage) or {})
    for key, value in stage_values.items():
        field = f"{stage}.{key}"
        seen.add(field)
        if isinstance(value, list):
            lines.append(f"{key}: {len(value)} record(s)")
        else:
            lines.append(f"{key}: {value}")
    for field in sorted(
        item for item in missing if _stage_for_field(item, plan) == stage
    ):
        if field not in seen:
            lines.append(f"{field.rsplit('.', 1)[-1]}: missing")
    return lines


def _draft_stage_attention(
    stage: str,
    plan: AskPlanState,
    summary: dict[str, Any],
    attention_items: list[tuple[str, str]],
) -> list[str]:
    """Collect attention items for one stage.

    Filtering stays inline instead of using a pre-aggregated mapping.
    """
    attention: list[str] = []
    for item_stage, message in attention_items:
        if item_stage == stage:
            attention.append(message)
    # Add stage plan details
    for detail in summary.get("stage_plan_details") or []:
        if str(detail.get("stage") or "") == stage:
            for note in detail.get("inputs") or []:
                attention.append(str(note))
    # Add warnings for this stage
    for warning in summary.get("warnings") or []:
        warning_stage = _stage_for_warning(str(warning), plan)
        if warning_stage == stage:
            attention.append(str(warning))
    return attention


def _runtime_points_for_stage(
    summary: dict[str, Any], stage: str
) -> list[dict[str, Any]]:
    return [
        point
        for point in summary.get("runtime_decision_points") or []
        if point.get("stage") == stage
    ]


def _runtime_parameter_contracts_for_stage(
    summary: dict[str, Any], stage: str
) -> list[dict[str, Any]]:
    return [
        contract
        for contract in summary.get("runtime_parameter_contracts") or []
        if contract.get("stage") == stage
    ]


def _deferred_parameter_names(summary: dict[str, Any], method: str) -> set[str]:
    for contract in summary.get("runtime_parameter_contracts") or []:
        if contract.get("method") == method:
            return set(contract.get("parameter_names") or [])
    return set()


def _draft_attention_items(
    plan: AskPlanState, summary: dict[str, Any]
) -> list[tuple[str, str]]:
    items = list(_draft_blocking_items(plan, summary))
    return list(dict.fromkeys(items))


def _draft_blocking_items(
    plan: AskPlanState, summary: dict[str, Any]
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for occurrence in plan.selected_stage_occurrences or []:
        if not occurrence.allow_local_execution:
            continue
        occurrence_stage = occurrence.stage
        if (
            occurrence_stage in _STAGES_NEEDING_METHOD_SELECTION
            and not _selected_methods_for_stage(plan, occurrence_stage)
        ):
            items.append((occurrence_stage, "method is missing"))
    for field in summary.get("missing_parameter_fields") or []:
        field_stage = _stage_for_field(str(field), plan)
        items.append((field_stage, f"{_field_label(str(field))} is missing"))
    if summary.get("run_config_preview_error"):
        preview_stage = (plan.selected_stage_names or ["plan"])[0]
        items.append(
            (
                preview_stage,
                f"run config is not ready: {summary['run_config_preview_error']}",
            )
        )
    items.extend(_next_clarification_blocking_items(plan, summary))
    return list(dict.fromkeys(items))


def _next_clarification_blocking_items(
    plan: AskPlanState, summary: dict[str, Any]
) -> list[tuple[str, str]]:
    clarification = next_clarification(plan, ask_plan_ref="<draft>")
    if clarification is None or clarification.kind == "confirm":
        return []
    if clarification.kind == "stage_selection":
        return [("plan", "stage list is not selected")]
    if clarification.kind == "method_selection":
        stages = sorted((clarification.choices.get("methods_by_stage") or {}).keys())
        return [(stage, "method is missing") for stage in stages]
    if clarification.kind == "parameter_values":
        return []
    return [("plan", f"{clarification.kind} is still required")]


def _render_draft_blockers(blocking_items: list[tuple[str, str]]) -> None:
    print("\nCannot continue yet. Fix these items in the plan first:")
    for stage, message in blocking_items:
        print(f"  - {stage}: {message}")


def _prompt_draft_action(plan: AskPlanState) -> str:
    stages = list(plan.selected_stage_names or [])
    if _HAS_QUESTIONARY:
        choices = [
            questionary.Choice("continue", value="continue"),
            *[
                questionary.Choice(f"edit {idx}. {stage}", value=f"stage:{idx - 1}")
                for idx, stage in enumerate(stages, 1)
            ],
            questionary.Choice("edit stage list", value="stages"),
            questionary.Choice("discard and regenerate draft", value="restart"),
        ]
        answer = _safe_ask(
            questionary.select("Plan action:", choices=choices, default="continue")
        )
        return str(answer)

    raw = input("plan> ").strip().lower()
    if raw in ("", "c", "continue", "y", "yes"):
        return "continue"
    if raw in ("s", "stage", "stages"):
        return "stages"
    if raw in ("r", "restart", "n", "no"):
        return "restart"
    if len(raw) > 1 and raw[:-1].isdigit() and raw[-1] in {"m", "p"}:
        idx = int(raw[:-1]) - 1
        if 0 <= idx < len(stages):
            if raw[-1] == "m" and stages[idx] in _FIXED_METHOD_STAGES:
                return f"method:{stages[idx]}"
            return ("method:" if raw[-1] == "m" else "params:") + stages[idx]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(stages):
            return f"stage:{idx}"
    return raw


def _edit_draft_stage(plan: AskPlanState, occurrence_index: int) -> AskPlanState:
    current = plan
    invalid_attempts = 0
    while True:
        occurrence = (current.selected_stage_occurrences or [])[occurrence_index]
        stage = occurrence.stage
        summary = build_run_plan_summary(current)
        print()
        print("-" * 64)
        print(stage)
        print("-" * 64)
        attention_items = _draft_attention_items(current, summary)
        attention = _draft_stage_attention(stage, current, summary, attention_items)
        _render_draft_stage_summary(
            occurrence_index + 1,
            stage,
            current,
            summary,
            attention,
            nested=True,
            occurrence_index=occurrence_index,
        )
        print("\nActions:")
        print("  l      change local execution authorization")
        if stage not in _FIXED_METHOD_STAGES:
            print("  m      change method")
        print("  p      edit parameters")
        print("  b      back to plan")
        raw = input(f"{stage}> ").strip().lower()
        if raw in ("", "b", "back"):
            return current
        if raw in ("l", "local", "local-execution"):
            current = _edit_occurrence_local_execution(current, occurrence_index)
            continue
        if raw in ("m", "method", "methods"):
            if stage in _FIXED_METHOD_STAGES:
                print(
                    f"{stage} uses {_fixed_pipeline_description(stage)}; "
                    "there is no method choice."
                )
                continue
            invalid_attempts = 0
            current = _edit_stage_methods(current, stage)
            continue
        if raw in ("p", "param", "params", "parameters"):
            invalid_attempts = 0
            current = _edit_stage_parameters(current, stage)
            continue
        invalid_attempts += 1
        allowed = "l, p, or b" if stage in _FIXED_METHOD_STAGES else "l, m, p, or b"
        print(f"Unknown action. Use {allowed}.")
        if invalid_attempts >= _MAX_INVALID_PROMPT_ATTEMPTS:
            print("Too many invalid actions; returning to the plan.")
            return current


def _edit_draft_stages(plan: AskPlanState) -> AskPlanState:
    req = AskClarificationRequest(
        status="clarification_required",
        kind="stage_selection",
        session_id=plan.session_id,
        ask_plan_ref="<draft>",
        manifest_ref=plan.manifest_ref,
        question="Which workflow stages should run?",
        choices={
            "available_stages": _all_selectable_stages(),
            "suggested_stage_occurrences": [
                occurrence.model_dump(mode="json")
                for occurrence in plan.selected_stage_occurrences or []
            ],
        },
        required_fields=["selected_stage_occurrences"],
        response_schema={
            "kind": "stage_selection",
            "selected_stage_occurrences": [
                occurrence.model_dump(mode="json")
                for occurrence in plan.selected_stage_occurrences or []
            ],
        },
        next_action={},
    )
    response = _prompt_stage_selection(req)
    return _refresh_draft_defaults(merge_clarification_response(plan, response))


def _edit_occurrence_local_execution(
    plan: AskPlanState, occurrence_index: int
) -> AskPlanState:
    occurrences = list(plan.selected_stage_occurrences or [])
    occurrence = occurrences[occurrence_index]
    prompt = (
        "Allow local execution for occurrence "
        f"{occurrence_index + 1} ({occurrence.stage})?"
    )
    if _HAS_QUESTIONARY:
        allowed = bool(
            _safe_ask(
                questionary.confirm(
                    prompt, default=occurrence.allow_local_execution
                )
            )
        )
    else:
        default = "Y/n" if occurrence.allow_local_execution else "y/N"
        raw = input(f"{prompt} [{default}] ").strip().lower()
        allowed = occurrence.allow_local_execution if not raw else raw in {"y", "yes"}
    occurrences[occurrence_index] = occurrence.model_copy(
        update={"allow_local_execution": allowed}
    )
    return plan.model_copy(
        update={"selected_stage_occurrences": occurrences, "confirmed": False}
    )


def _edit_stage_methods(plan: AskPlanState, stage: str) -> AskPlanState:
    if stage in _FIXED_METHOD_STAGES:
        print(
            f"\n{stage} uses {_fixed_pipeline_description(stage)}; "
            "there is no method choice."
        )
        return plan
    methods = _executable_methods_for_stage(stage)
    if not methods:
        print(f"\nNo method choices are registered for {stage}.")
        return plan
    current = (plan.selected_methods or {}).get(stage)
    default = current if current else _default_methods_for_stage(stage)
    req = AskClarificationRequest(
        status="clarification_required",
        kind="method_selection",
        session_id=plan.session_id,
        ask_plan_ref="<draft>",
        manifest_ref=plan.manifest_ref,
        question=f"Which methods should run for {stage}?",
        choices={
            "methods_by_stage": {
                stage: [
                    {
                        "method_name": m["method_name"],
                        "selection_role": m.get("selection_role"),
                        "display_name": m.get("display_name"),
                    }
                    for m in methods
                ]
            },
            "defaults": {stage: default} if default else {},
        },
        required_fields=["selected_methods"],
        response_schema={
            "kind": "method_selection",
            "selected_methods": {stage: default},
        },
        next_action={},
    )
    response = _prompt_method_selection(req)
    return _refresh_draft_defaults(merge_clarification_response(plan, response))


def _edit_stage_parameters(plan: AskPlanState, stage: str) -> AskPlanState:
    summary = build_run_plan_summary(plan)
    fields = _editable_parameter_fields_for_stage(plan, stage, summary)
    if not fields:
        print(f"\nNo editable parameters are known for {stage}.")
        return plan
    req = AskClarificationRequest(
        status="clarification_required",
        kind="parameter_values",
        session_id=plan.session_id,
        ask_plan_ref="<draft>",
        manifest_ref=plan.manifest_ref,
        question=f"Edit parameters for {stage}.",
        choices={
            "defaults": _filter_method_map_for_stage(
                summary.get("parameter_defaults") or {}, plan, stage
            ),
            "shared_defaults": summary.get("shared_parameter_defaults") or {},
            "stage_defaults": {
                stage: (summary.get("stage_parameter_defaults") or {}).get(stage) or {}
            },
        },
        required_fields=fields,
        response_schema={"kind": "parameter_values"},
        next_action={},
    )
    response = _prompt_parameter_values(req, plan)
    return _refresh_draft_defaults(merge_clarification_response(plan, response))


def _editable_parameter_fields_for_stage(
    plan: AskPlanState,
    stage: str,
    summary: dict[str, Any],
) -> list[str]:
    fields: list[str] = []
    if stage == _shared_parameter_stage(plan):
        shared_keys = set((summary.get("shared_parameter_defaults") or {}).keys())
        shared_keys.update((summary.get("shared_parameters") or {}).keys())
        for field in summary.get("missing_parameter_fields") or []:
            if str(field).startswith("shared_parameters."):
                shared_keys.add(str(field).rsplit(".", 1)[-1])
        fields.extend(f"shared_parameters.{key}" for key in sorted(shared_keys))

    defaults = summary.get("parameter_defaults") or {}
    params = summary.get("method_parameters") or {}
    for method in _selected_methods_for_stage(plan, stage):
        keys = set((defaults.get(method) or {}).keys())
        keys.update((params.get(method) or {}).keys())
        for field in summary.get("missing_parameter_fields") or []:
            if str(field).startswith(f"{method}."):
                keys.add(str(field).rsplit(".", 1)[-1])
        fields.extend(f"{method}.{key}" for key in sorted(keys))

    stage_keys = set(
        ((summary.get("stage_parameter_defaults") or {}).get(stage) or {}).keys()
    )
    stage_keys.update(((summary.get("stage_parameters") or {}).get(stage) or {}).keys())
    for field in summary.get("missing_parameter_fields") or []:
        field_text = str(field)
        method_scoped = any(
            field_text.startswith(f"{method}.")
            for method in _selected_methods_for_stage(plan, stage)
        )
        if (
            _stage_for_field(field_text, plan) == stage
            and field_text.startswith(f"{stage}.")
            and not method_scoped
        ):
            stage_keys.add(field_text.rsplit(".", 1)[-1])
    fields.extend(f"{stage}.{key}" for key in sorted(stage_keys))
    return list(dict.fromkeys(fields))


def _filter_method_map_for_stage(
    values: dict[str, dict[str, Any]],
    plan: AskPlanState,
    stage: str,
) -> dict[str, dict[str, Any]]:
    methods = set(_selected_methods_for_stage(plan, stage))
    return {method: params for method, params in values.items() if method in methods}


def _refresh_draft_defaults(plan: AskPlanState) -> AskPlanState:
    return _auto_fill_inferred_defaults(_fill_draft_default_methods(plan))


def _selected_methods_for_stage(plan: AskPlanState, stage: str) -> list[str]:
    selected = (plan.selected_methods or {}).get(stage)
    if (
        not selected
        and stage == "assembly_review"
        and stage in (plan.selected_stage_names or [])
    ):
        selected = _default_methods_for_stage(stage)
    if not selected:
        return []
    return selected if isinstance(selected, list) else [selected]


def _shared_parameter_stage(plan: AskPlanState) -> str | None:
    order = list(plan.selected_stage_names or [])
    for preferred in ("assembly", "genome_survey", "read_qc"):
        if preferred in order:
            return preferred
    return order[0] if order else None


def _stage_for_field(field: str, plan: AskPlanState) -> str:
    """Determine which stage a parameter field belongs to.

    TODO(refactor): Replace hardcoded prefix checks with structured field metadata.
    See code review finding #8.
    """
    if field.startswith("shared_parameters."):
        return _shared_parameter_stage(plan) or "plan"
    for stage, methods in (plan.selected_methods or {}).items():
        method_list = methods if isinstance(methods, list) else [methods]
        for method in method_list:
            if field.startswith(f"{method}."):
                return stage
    stage, _, _ = field.partition(".")
    return stage or "plan"


def _stage_for_warning(warning: str, plan: AskPlanState) -> str:
    for stage in plan.selected_stage_names or []:
        if warning.startswith(f"{stage}:") or f"{stage}." in warning:
            return stage
    return _shared_parameter_stage(plan) or "plan"


def _field_label(field: str) -> str:
    if field.startswith("shared_parameters."):
        return field.rsplit(".", 1)[-1]
    return field.rsplit(".", 1)[-1]


def _render_plan_summary(
    summary: dict[str, Any],
    *,
    title: str,
    preview: bool = False,
    draft: AskPlanState | None = None,
) -> None:
    explicit_shared = _explicit_shared_params(draft) if draft is not None else set()
    print()
    print("=" * 50)
    print(title)
    print("=" * 50)

    print(f"\nSession:  {summary.get('session_id', '?')}")
    print(f"Data dir: {summary.get('data_dir', '?')}")
    print(f"Manifest: {summary.get('manifest_ref', '?')}")
    parse_source = summary.get("intent_parse_source")
    if parse_source:
        parse_label = {
            "llm": "LLM",
            "llm_plan_draft": "LLM Plan Draft",
            "deterministic_fallback": "local deterministic fallback",
        }.get(str(parse_source), str(parse_source))
        print(f"Intent parser: {parse_label}")
    _render_summary_inputs(summary)

    roles = summary.get("stage_roles", [])
    order = summary.get("selected_stage_names", [])
    if order:
        print("\nStages:")
        for index, stage in enumerate(order):
            role = roles[index] if index < len(roles) else ""
            label = _ROLE_LABELS.get(role, role)
            print(f"  [{label:>9}] {stage}")
    else:
        print("\nStages: Inventory only")

    methods = summary.get("selected_methods", {})
    method_note = "  (recommended)" if draft is not None else ""
    method_stages = [stage for stage in order if stage not in _FIXED_METHOD_STAGES]
    if preview and method_stages:
        print("\nMethods:")
        for stage in method_stages:
            value = methods.get(stage)
            if not value:
                print(f"  {stage}: pending")
                continue
            method_list = value if isinstance(value, list) else [value]
            for m in method_list:
                print(f"  {stage}: {m}{method_note}")
    elif methods:
        visible_methods = {
            stage: value
            for stage, value in methods.items()
            if stage not in _FIXED_METHOD_STAGES
        }
        if visible_methods:
            print("\nMethods:")
        for stage, value in visible_methods.items():
            method_list = value if isinstance(value, list) else [value]
            for m in method_list:
                print(f"  {stage}: {m}")

    missing_fields = list(summary.get("missing_parameter_fields") or [])
    shared_defaults = summary.get("shared_parameter_defaults", {}) if preview else {}
    pending_shared = _pending_shared_parameter_names(missing_fields)
    deferred_shared = summary.get("deferred_shared_parameters", {}) if preview else {}
    shared_sources = (
        summary.get("shared_parameter_default_sources", {}) if preview else {}
    )
    shared = {**shared_defaults, **(summary.get("shared_parameters", {}) or {})}
    if shared or deferred_shared or (preview and pending_shared):
        print("\nShared parameters:")
        for k, v in shared.items():
            note = ""
            if draft is not None:
                note = (
                    "  (from request)"
                    if k in explicit_shared
                    else _source_note(shared_sources.get(k), fallback="recommended")
                )
            elif preview and k in shared_sources:
                note = _source_note(shared_sources.get(k), fallback="recommended")
            print(f"  {k}: {v}{note}")
        if preview:
            for k, item in deferred_shared.items():
                if k in shared:
                    continue
                value = item.get("value", "auto") if isinstance(item, dict) else "auto"
                source = item.get("source") if isinstance(item, dict) else "auto"
                print(f"  {k}: {value}{_source_note(source, fallback='auto')}")
        if preview:
            for k in pending_shared:
                if k not in shared and k not in deferred_shared:
                    print(f"  {k}: pending")
    elif preview:
        print("\nShared parameters: pending")

    params = summary.get("method_parameters", {})
    if preview:
        _render_preview_parameters(
            summary, params, recommended_params=draft is not None
        )
    elif params:
        print("\nParameters:")
        for method, pdict in params.items():
            short = method.rsplit(".", 1)[-1]
            for k, v in pdict.items():
                print(f"  [{short}] {k}: {v}")

    stage_defaults = summary.get("stage_parameter_defaults", {}) if preview else {}
    pending_stage = _pending_stage_parameter_names(missing_fields)
    stage_params = {**stage_defaults, **(summary.get("stage_parameters", {}) or {})}
    if stage_params or (preview and pending_stage):
        print("\nStage parameters:")
        for stage, pdict in stage_params.items():
            for k, v in pdict.items():
                if isinstance(v, list):
                    print(f"  [{stage}] {k}: {len(v)} record(s)")
                else:
                    print(f"  [{stage}] {k}: {v}")
        if preview:
            for stage, fields in pending_stage.items():
                existing = stage_params.get(stage, {})
                for field in fields:
                    if field not in existing:
                        print(f"  [{stage}] {field}: pending")

    if preview and summary.get("stage_plan_details"):
        _render_stage_plan_details(summary["stage_plan_details"])

    runtime_parameter_contracts = summary.get("runtime_parameter_contracts") or []
    if runtime_parameter_contracts:
        print("\nRuntime-derived parameters:")
        for contract in runtime_parameter_contracts:
            print(f"  [{contract.get('stage')}] {contract.get('method')}: deferred")
            print(
                "    values are recommended after prerequisite results exist and "
                f"confirmed at {contract.get('trigger_gate', {}).get('gate_id')}"
            )
            constraints = contract.get("user_constraints") or {}
            if constraints:
                print(f"    user constraints (not frozen values): {constraints}")

    decision_points = summary.get("runtime_decision_points") or []
    if decision_points:
        print("\nRuntime human gates (proposals are not available yet):")
        for point in decision_points:
            print(
                f"  [{point.get('stage')}] {point.get('label')}: "
                f"{point.get('status', 'deferred')}"
            )
            print(f"    trigger: {point.get('trigger')}")
            recommendation_status = point.get("parameter_recommendation_status")
            if recommendation_status and recommendation_status != "not_applicable":
                print(f"    parameter recommendation: {recommendation_status}")
        print(
            "  The TUI pauses at each reached gate, shows its runtime "
            "evidence/proposal, and asks for a decision."
        )

    warnings = summary.get("warnings", [])
    for w in warnings:
        print(f"\n  warning: {w}")

    preview_error = summary.get("run_config_preview_error")
    if preview_error:
        print(f"\nRun-config error: {preview_error}")

    print("=" * 50)
    if preview and draft is None:
        print("Advisory preview. Remaining choices and a final confirmation follow.")


def _render_preview_parameters(
    summary: dict[str, Any],
    params: dict[str, dict[str, Any]],
    *,
    recommended_params: bool = False,
) -> None:
    defaults = summary.get("parameter_defaults", {}) or {}
    sources = summary.get("parameter_default_sources", {}) or {}
    missing_fields = list(summary.get("missing_parameter_fields") or [])
    method_order = _ordered_summary_methods(summary)
    pending_by_method = _pending_method_parameter_names(
        missing_fields, set(method_order) | set(defaults) | set(params)
    )
    combined: dict[str, dict[str, Any]] = {}
    for method, values in defaults.items():
        combined[method] = dict(values or {})
    for method, values in params.items():
        merged = dict(combined.get(method) or {})
        merged.update(values or {})
        combined[method] = merged
    render_order = method_order + [m for m in combined if m not in method_order]
    render_order += [m for m in pending_by_method if m not in render_order]

    lines: list[str] = []
    for method in render_order:
        values = combined.get(method, {})
        pending = pending_by_method.get(method, [])
        if not values and not pending:
            continue
        short = method.rsplit(".", 1)[-1]
        default_values = defaults.get(method, {}) or {}
        default_sources = sources.get(method, {}) or {}
        explicit_values = params.get(method, {}) or {}
        deferred_names = _deferred_parameter_names(summary, method)
        for key, value in values.items():
            if key in deferred_names:
                continue
            note = ""
            if key in explicit_values and not recommended_params:
                note = ""
            elif key in default_values:
                note = _source_note(default_sources.get(key), fallback="recommended")
            lines.append(f"  [{short}] {key}: {value}{note}")
        for key in pending:
            if key not in values:
                lines.append(f"  [{short}] {key}: pending")

    if lines:
        print("\nParameters:")
        for line in lines:
            print(line)
    else:
        print("\nParameters: pending")


def _ordered_summary_methods(summary: dict[str, Any]) -> list[str]:
    methods = summary.get("selected_methods", {}) or {}
    order = summary.get("selected_stage_names", []) or list(methods)
    ordered: list[str] = []
    for stage in order:
        value = methods.get(stage)
        if not value:
            continue
        method_list = value if isinstance(value, list) else [value]
        for method in method_list:
            if method not in ordered:
                ordered.append(method)
    return ordered


class _MissingFieldScopes(TypedDict):
    shared: list[str]
    stage: dict[str, list[str]]
    method: dict[str, list[str]]


def _group_missing_fields_by_scope(
    missing_fields: list[str], methods: set[str] | None = None
) -> _MissingFieldScopes:
    """Parse missing_fields and group by scope: shared, stage, or method.

    Returns dict with keys:
        - shared: list[str] of shared parameter names
          (without 'shared_parameters.' prefix)
        - stage: dict[str, list[str]] mapping stage name to parameter names
        - method: dict[str, list[str]] mapping method name to parameter names

    Fields that match no scope (no 'shared_parameters.' prefix, no known method when
    ``methods`` is given, and not exactly ``stage.param`` shaped) are dropped and logged
    at debug level rather than silently discarded.
    """
    shared: list[str] = []
    stage: dict[str, list[str]] = {}
    method: dict[str, list[str]] = {}

    for field in missing_fields:
        if field.startswith("shared_parameters."):
            shared.append(field[len("shared_parameters.") :])
        elif methods:
            # Try to match against known methods
            matched = False
            for m in methods:
                prefix = f"{m}."
                if field.startswith(prefix):
                    method.setdefault(m, []).append(field[len(prefix) :])
                    matched = True
                    break
            if not matched:
                # Fall back to stage-scoped
                parts = field.split(".")
                if len(parts) == 2:
                    stage.setdefault(parts[0], []).append(parts[1])
                else:
                    logger.debug(
                        "Dropping unclassifiable missing field %r "
                        "(no matching method, not stage-scoped)",
                        field,
                    )
        else:
            # No methods provided, assume stage-scoped
            parts = field.split(".")
            if len(parts) == 2:
                stage.setdefault(parts[0], []).append(parts[1])
            else:
                logger.debug(
                    "Dropping unclassifiable missing field %r (not stage-scoped)", field
                )

    return {"shared": shared, "stage": stage, "method": method}


def _pending_shared_parameter_names(missing_fields: list[str]) -> list[str]:
    """Extract shared parameter names from missing_fields."""
    return _group_missing_fields_by_scope(missing_fields)["shared"]


def _pending_stage_parameter_names(missing_fields: list[str]) -> dict[str, list[str]]:
    """Extract stage-scoped parameter names from missing_fields."""
    return _group_missing_fields_by_scope(missing_fields)["stage"]


def _pending_method_parameter_names(
    missing_fields: list[str], methods: set[str]
) -> dict[str, list[str]]:
    """Extract method-scoped parameter names from missing_fields."""
    return _group_missing_fields_by_scope(missing_fields, methods=methods)["method"]


def _source_note(source: str | None, *, fallback: str) -> str:
    if not source:
        return f"  ({fallback})"
    if source.startswith("auto:"):
        return f"  ({source})"
    if source.startswith("evidence:"):
        return f"  ({source})"
    if source == "default":
        return "  (default)"
    if source == "from request":
        return "  (from request)"
    if source == "from manifest":
        return "  (from manifest)"
    return f"  ({source})"


def _render_stage_plan_details(details: list[dict[str, Any]]) -> None:
    print("\nStage details:")
    for item in details:
        stage = item.get("stage", "?")
        status = item.get("status") or "pending"
        role = _ROLE_LABELS.get(str(item.get("role") or ""), item.get("role") or "")
        suffix = f" [{role}]" if role else ""
        print(f"  {stage}:{suffix} {status}")
        for input_note in item.get("inputs") or []:
            print(f"    input: {input_note}")
        methods = item.get("methods") or []
        if methods:
            for method in methods:
                method_name = method.get("method", "?")
                print(f"    method: {method_name}")
                params = method.get("parameters") or {}
                missing = method.get("missing_parameters") or []
                if params:
                    short = method_name.rsplit(".", 1)[-1]
                    rendered = ", ".join(
                        f"{key}={value}" for key, value in params.items()
                    )
                    print(f"      params [{short}]: {rendered}")
                if missing:
                    print(f"      pending: {', '.join(str(name) for name in missing)}")
        missing_stage = item.get("missing_parameters") or []
        if missing_stage:
            print(f"    pending: {', '.join(str(name) for name in missing_stage)}")


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def _safe_ask(question: Any) -> Any:
    """Call questionary .ask() with Ctrl+C / Esc handling."""
    try:
        result = question.ask()
    except KeyboardInterrupt:
        raise _UserAbort()
    if result is None:
        raise _UserAbort()
    return result


def _prompt_for_answer(
    req: AskClarificationRequest, plan: AskPlanState
) -> AskClarificationResponse:
    if req.kind == "stage_selection":
        return _prompt_stage_selection(req)
    if req.kind == "method_selection":
        return _prompt_method_selection(req)
    if req.kind == "parameter_values":
        return _prompt_parameter_values(req, plan)
    raise ValueError(f"unexpected clarification kind: {req.kind}")


def _prompt_confirm(message: str) -> bool:
    if _HAS_QUESTIONARY:
        return bool(_safe_ask(questionary.confirm(message, default=False)))
    raw = input(f"{message} [y/N] ").strip().lower()
    return raw in ("y", "yes")


def _explicit_shared_params(draft: AskPlanState) -> set[str]:
    """Shared-parameter keys that were parsed from the user request, not inferred."""
    entry = draft.entry_request or {}
    explicit: set[str] = set()
    if entry.get("platform") and entry["platform"] != "unknown":
        explicit.add("platform")
    if entry.get("genome_size_estimate"):
        explicit.add("estimated_genome_size")
    return explicit


def _prompt_stage_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    available = req.choices.get("available_stages", [])
    suggested = req.choices.get("suggested_stage_occurrences", [])
    suggested_names = [str(item["stage"]) for item in suggested]
    print("\n--- Step 1: Stage Selection ---")
    print("  Enter the complete ordered sequence; repeated stages are allowed.\n")
    print("Available stages:")
    for i, stage in enumerate(available, 1):
        desc = _STAGE_DESCRIPTIONS.get(stage, "")
        desc_str = f"  {desc}" if desc else ""
        print(f"  {i}. {stage}{desc_str}")
    if suggested_names:
        print(f"\n  Recommended: {', '.join(suggested_names)}")
    else:
        print("\n  Recommended: Inventory only (no biological stages)")
    print("\n  Enter numbers or names, e.g. 1,3,1 or t2t,assembly_review,t2t")
    print("  Press Enter to accept the recommendation, including Inventory only.")
    if _HAS_QUESTIONARY:
        raw = str(
            _safe_ask(
                questionary.text(
                    "Ordered stage sequence:", default=",".join(suggested_names)
                )
            )
        ).strip()
    else:
        raw = input("> ").strip()
    stages = _parse_selection(raw, available) if raw else suggested_names
    occurrences: list[PlanStageOccurrence] = []
    for index, stage in enumerate(stages):
        suggested_allowed = (
            bool(suggested[index].get("allow_local_execution", True))
            if index < len(suggested) and suggested[index].get("stage") == stage
            else True
        )
        prompt = f"Allow local execution for occurrence {index + 1} ({stage})?"
        if _HAS_QUESTIONARY:
            allowed = bool(
                _safe_ask(questionary.confirm(prompt, default=suggested_allowed))
            )
        else:
            default = "Y/n" if suggested_allowed else "y/N"
            answer = input(f"{prompt} [{default}] ").strip().lower()
            allowed = suggested_allowed if not answer else answer in {"y", "yes"}
        occurrences.append(
            PlanStageOccurrence(
                stage=cast(RequestableStage, stage),
                allow_local_execution=allowed,
            )
        )
    return AskClarificationResponse(
        kind="stage_selection", selected_stage_occurrences=occurrences
    )


def _prompt_method_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    methods_by_stage = req.choices.get("methods_by_stage", {})
    selected: dict[str, str | list[str]] = {}

    print("\n--- Step 2: Method Selection ---\n")

    for stage, methods in methods_by_stage.items():
        method_names = [m["method_name"] for m in methods]
        if stage == "read_qc":
            selected[stage] = _prompt_read_qc_filter_choice(
                methods, req.choices.get("defaults", {}).get(stage)
            )
            continue
        requested_default = req.choices.get("defaults", {}).get(stage)
        if isinstance(requested_default, list):
            default_checked = [m for m in requested_default if m in method_names]
        elif requested_default:
            default_checked = (
                [requested_default] if requested_default in method_names else []
            )
        else:
            default_checked = []
        # The assembly graph executes one primary candidate at a time. Keep
        # its interactive choice singular even though other stages may run
        # several methods in one pass.
        if not default_checked:
            default_checked = method_names[:1]

        if _HAS_QUESTIONARY:
            choices = []
            for m in methods:
                choices.append(
                    questionary.Choice(
                        m["method_name"],
                        value=m["method_name"],
                        checked=(m["method_name"] in default_checked),
                    )
                )
            if stage == "assembly":
                selected[stage] = _safe_ask(
                    questionary.select(
                        f"Primary method for {stage}:",
                        choices=choices,
                        default=default_checked[0],
                        instruction="(choose one primary)",
                    )
                )
            else:
                answer = _safe_ask(
                    questionary.checkbox(
                        f"Methods for {stage}:",
                        choices=choices,
                        instruction="(Space toggle, Enter confirm)",
                    )
                )
                if not answer:
                    answer = default_checked
                selected[stage] = answer[0] if len(answer) == 1 else answer
        else:
            print(f"  Methods for [{stage}]:")
            for i, m in enumerate(methods, 1):
                marker = "*" if m["method_name"] in default_checked else " "
                print(f"    {marker} {i}. {m['method_name']}")
            prompt = (
                "Choose one number or name"
                if stage == "assembly"
                else "Enter numbers or names"
            )
            print(f"\n  {prompt}. Enter = {', '.join(default_checked)}:")
            raw = input(f"  {stage}> ").strip()
            if not raw:
                picks = default_checked
            else:
                picks = _parse_selection(raw, method_names)
            selected[stage] = (
                picks[0] if stage == "assembly" or len(picks) == 1 else picks
            )

    return AskClarificationResponse(kind="method_selection", selected_methods=selected)


def _prompt_read_qc_filter_choice(
    methods: list[dict[str, Any]], default: str | list[str] | None
) -> list[str]:
    default_methods = (
        default if isinstance(default, list) else [default] if default else []
    )
    default_filter = next(
        (method for method in default_methods if method in READ_QC_FILTER_METHODS),
        READ_QC_FILTER_SKIP,
    )
    filters = [m for m in methods if m.get("method_name") in READ_QC_FILTER_METHODS]
    filter_names = [m["method_name"] for m in filters]

    if _HAS_QUESTIONARY:
        choices = [
            questionary.Choice(
                m.get("display_name") or m["method_name"],
                value=m["method_name"],
            )
            for m in filters
        ]
        answer = _safe_ask(
            questionary.select(
                "Read QC length filtering:",
                choices=choices,
                default=default_filter,
                instruction="(↑↓ navigate, Enter confirm)",
            )
        )
        filter_method = answer or default_filter
    else:
        print("  Methods for [read_qc]:")
        print(f"    * always: {READ_QC_STATS_METHOD}")
        print("    length filtering:")
        for i, m in enumerate(filters, 1):
            label = m.get("display_name") or m["method_name"]
            marker = "*" if m["method_name"] == default_filter else " "
            print(f"    {marker} {i}. {label}")
        print(f"\n  Choose one length filter. Enter = {default_filter}:")
        raw = input("  read_qc length_filter> ").strip()
        picks = _parse_selection(raw, filter_names) if raw else [default_filter]
        filter_method = picks[0] if picks else default_filter

    return [READ_QC_STATS_METHOD, filter_method]


def _prompt_parameter_values(
    req: AskClarificationRequest, plan: AskPlanState
) -> AskClarificationResponse:
    defaults = req.choices.get("defaults", {})
    shared_defaults = dict(req.choices.get("shared_defaults", {}))
    stage_defaults = dict(req.choices.get("stage_defaults", {}))
    params: dict[str, dict[str, Any]] = {}
    stage_params: dict[str, dict[str, Any]] = stage_defaults
    shared_params: dict[str, Any] = {}
    print("\n--- Step 3: Parameters ---")
    print("  Press Enter to accept [default], or type a new value.\n")
    handled: set[str] = set()
    for field in req.required_fields:
        if field in handled:
            continue
        method_key, _, param_name = field.rpartition(".")
        if not method_key:
            method_key = param_name
        short_method = method_key.rsplit(".", 1)[-1]
        stage_name = method_key.split(".")[0] if "." in method_key else method_key
        label = f"[{stage_name} / {short_method}] {param_name}"

        hint = _PARAM_HINTS.get(f"{method_key}.{param_name}") or _PARAM_HINTS.get(
            param_name
        )
        if hint:
            label = f"{label}  ({hint})"

        if method_key == "shared_parameters":
            current_shared_defaults = _derive_shared_parameter_defaults(
                {**shared_defaults, **shared_params}
            )
            default = current_shared_defaults.get(param_name)
            if (
                param_name == "read_type"
                and default
                and current_shared_defaults.get("platform")
            ):
                if not _shared_parameter_value_is_valid("read_type", default):
                    default = None
                else:
                    print(
                        "  [shared_parameters] read_type derived from "
                        f"platform={current_shared_defaults.get('platform')}: "
                        f"{default}"
                    )
                    shared_params[param_name] = default
                    continue
        else:
            default = _find_default(field, defaults)

        if method_key == "shared_parameters":
            value = _prompt_shared_parameter(label, param_name, default)
            shared_params[param_name] = value
            shared_params = _derive_shared_parameter_defaults(shared_params)
        else:
            value = _prompt_text_value(label, default, required=True)
            if param_name == "estimated_genome_size" and isinstance(value, str):
                value = _parse_genome_size(value)
            params.setdefault(method_key, {})[param_name] = value
            _update_derived_defaults(method_key, param_name, value, defaults)
    normalized_shared = (
        SharedParameters.model_validate(
            _derive_shared_parameter_defaults(shared_params)
        )
        if shared_params
        else None
    )
    return AskClarificationResponse(
        kind="parameter_values",
        shared_parameters=normalized_shared,
        method_parameters=params or None,
        stage_parameters=stage_params or None,
    )


def _prompt_text_value(label: str, default: Any, *, required: bool = False) -> Any:
    invalid_attempts = 0
    while True:
        if _HAS_QUESTIONARY:
            answer = _safe_ask(
                questionary.text(
                    f"{label}:",
                    default=str(default) if default is not None else "",
                )
            )
        else:
            if default is not None:
                answer = input(f"  {label} [{default}]: ").strip()
            else:
                answer = input(f"  {label}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        if not required:
            return default
        invalid_attempts += 1
        print(f"  {label} is required.")
        if invalid_attempts >= _MAX_INVALID_PROMPT_ATTEMPTS:
            raise _UserAbort()


def _prompt_shared_parameter(label: str, param_name: str, default: Any) -> Any:
    if param_name == "platform":
        return _prompt_shared_platform(label, default)
    if param_name == "read_type":
        return _prompt_shared_read_type(label, default)

    while True:
        value = _prompt_text_value(label, default)
        if param_name == "estimated_genome_size" and isinstance(value, str):
            value = _parse_genome_size(value)
        if _shared_parameter_value_is_valid(param_name, value):
            return value


def _prompt_shared_platform(label: str, default: Any) -> Any:
    default_value = _normalize_shared_platform(default)
    if _HAS_QUESTIONARY:
        return _safe_ask(
            questionary.select(
                f"{label}:",
                choices=list(_SHARED_PLATFORM_VALUES),
                default=default_value
                if default_value in _SHARED_PLATFORM_VALUES
                else "unknown",
                instruction="(used for asset registry platform)",
            )
        )

    print("    platform choices:")
    for idx, value in enumerate(_SHARED_PLATFORM_VALUES, start=1):
        default_marker = " [default]" if value == default_value else ""
        print(f"      {idx}. {value}{default_marker}")
    while True:
        value = _prompt_text_value(label, default_value)
        normalized = _normalize_shared_platform(value)
        if _shared_parameter_value_is_valid("platform", normalized):
            return normalized


def _prompt_shared_read_type(label: str, default: Any) -> Any:
    default_value = _normalize_shared_read_type(default)
    if _HAS_QUESTIONARY:
        return _safe_ask(
            questionary.select(
                f"{label}:",
                choices=[
                    questionary.Choice(_read_type_choice_label(value), value=value)
                    for value in _SHARED_READ_TYPE_VALUES
                ],
                default=default_value
                if default_value in _SHARED_READ_TYPE_VALUES
                else None,
                instruction="(use normalized shared read type)",
            )
        )

    alias_text = ", ".join(
        f"{alias}->{value}" for alias, value in sorted(FLYE_TO_SHARED_READ_TYPE.items())
    )
    print(f"    accepted: {', '.join(_SHARED_READ_TYPE_VALUES)}")
    print(f"    Flye aliases accepted: {alias_text}")
    while True:
        value = _prompt_text_value(label, default_value)
        normalized = _normalize_shared_read_type(value)
        if _shared_parameter_value_is_valid("read_type", normalized):
            return normalized


def _read_type_choice_label(value: str) -> str:
    flye_aliases = sorted(
        alias
        for alias, normalized in FLYE_TO_SHARED_READ_TYPE.items()
        if normalized == value
    )
    suffix = f" (Flye: {', '.join(flye_aliases)})" if flye_aliases else ""
    return f"{value}{suffix}"


def _normalize_shared_read_type(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value).strip()
    return FLYE_TO_SHARED_READ_TYPE.get(raw, raw)


def _normalize_shared_platform(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value).strip().lower().replace("-", "_")
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(_SHARED_PLATFORM_VALUES):
            return _SHARED_PLATFORM_VALUES[index - 1]
    if raw in {"nanopore", "nano", "ont_hq"}:
        return "ont"
    if raw in {"pacbio_hifi", "pacbio_hifi_reads", "hifi", "ccs"}:
        return "pacbio_hifi"
    if raw in {"pacbio_clr", "pacbio_raw", "pacbio", "sequel"}:
        return "clr"
    if raw in {"cyclone_seq", "cyclone", "bgi_cycloneseq"}:
        return "cycloneseq"
    return raw


def _shared_parameter_value_is_valid(param_name: str, value: Any) -> bool:
    try:
        SharedParameters.model_validate({param_name: value})
    except ValidationError as exc:
        print(f"  Invalid {param_name}: {exc.errors()[0]['msg']}")
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_selection(raw: str, options: list[str]) -> list[str]:
    normalized = raw.replace("，", ",").replace("、", ",").replace(" ", ",")
    items = [s.strip() for s in normalized.split(",") if s.strip()]
    result: list[str] = []
    for item in items:
        if item.isdigit():
            idx = int(item) - 1
            if 0 <= idx < len(options):
                result.append(options[idx])
        else:
            for opt in options:
                if opt == item or opt.endswith(f".{item}"):
                    result.append(opt)
                    break
            else:
                result.append(item)
    return result


def _find_default(field: str, defaults: dict[str, Any]) -> Any:
    for method_key, pdict in defaults.items():
        if isinstance(pdict, dict):
            param = field.rsplit(".", 1)[-1]
            if param in pdict:
                return pdict[param]
    return None


def _update_derived_defaults(
    method_key: str, param_name: str, value: Any, defaults: dict[str, Any]
) -> None:
    """Derive related defaults after the user provides a value.

    For example, infer BUSCO lineage fields from its path.
    """
    if (
        method_key == "assembly_review.eval.busco"
        and param_name == "lineage_path"
        and value
    ):
        from pathlib import Path as _Path

        name = _Path(str(value)).name
        bucket = defaults.setdefault(method_key, {})
        bucket.setdefault("lineage_name", name)
        m = re.search(r"(odb\d+(?:\.\d+)?)$", name)
        if m:
            bucket.setdefault("lineage_version", m.group(1))
        bucket.setdefault("lineage_source", "local")


def _derive_shared_parameter_defaults(shared: dict[str, Any]) -> dict[str, Any]:
    if "read_type" in shared:
        return shared
    read_type = PLATFORM_TO_SHARED_READ_TYPE.get(str(shared.get("platform") or ""))
    if not read_type:
        return shared
    return {**shared, "read_type": read_type}


def _parse_genome_size(raw: str) -> int | str:
    m = _GENOME_SIZE_RE.match(raw.strip())
    if not m:
        return raw
    number = float(m.group(1))
    suffix = m.group(2).lower()
    multiplier = _GENOME_SIZE_MULTIPLIERS.get(suffix, 1)
    return int(number * multiplier)
