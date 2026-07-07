"""Interactive TTY loop for `gene-agent ask`."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.entrypoints.ask_planner import (
    FUNCTION_DATABASE_PROVENANCE_FIELD,
    READ_QC_FILTER_METHODS,
    READ_QC_FILTER_SKIP,
    READ_QC_HANDOFF_METHOD,
    READ_QC_STATS_METHOD,
    _STAGES_NEEDING_METHOD_SELECTION,
    _all_selectable_stages,
    _auto_fill_inferred_defaults,
    _default_methods_for_stage,
    _executable_methods_for_stage,
    _fill_draft_default_methods,
    build_initial_plan_draft,
    build_run_plan_summary,
    merge_clarification_response,
    next_clarification,
    save_plan_to_default_path,
)
from app.schemas.ask_plan import AskClarificationRequest, AskClarificationResponse, AskPlanState
from app.schemas.artifacts import FunctionDatabaseProvenance
from app.schemas.shared_parameters import (
    FLYE_TO_SHARED_READ_TYPE,
    PLATFORM_TO_SHARED_READ_TYPE,
    SHARED_TO_METHOD_READ_TYPE,
    SharedParameters,
)

try:
    import questionary
    _HAS_QUESTIONARY = True
except ImportError:
    _HAS_QUESTIONARY = False


logger = logging.getLogger(__name__)
_MAX_INVALID_PROMPT_ATTEMPTS = 3


class _UserAbort(Exception):
    pass


def is_interactive(args: argparse.Namespace) -> bool:
    if getattr(args, "json", False):
        return False
    if getattr(args, "non_interactive", False):
        return False
    return sys.stdin.isatty()


def run_interactive_loop(plan: AskPlanState, args: argparse.Namespace) -> int:
    try:
        return _interactive_loop_inner(plan, args)
    except (_UserAbort, KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1


def _reset_plan_selection(plan: AskPlanState) -> AskPlanState:
    return plan.model_copy(update={
        "selected_stages": None,
        "resolved_execution_order": None,
        "selected_methods": None,
        "shared_parameters": None,
        "method_parameters": None,
        "stage_parameters": None,
        "confirmed": False,
    })


def _interactive_loop_inner(plan: AskPlanState, args: argparse.Namespace) -> int:
    set_params = getattr(args, "set_params", None) or []

    if plan.selected_stages is None:
        draft = build_initial_plan_draft(plan)
        if draft is not None:
            draft = _apply_set_params(draft, set_params)
            reviewed = _review_plan_draft(draft)
            if reviewed is None:
                print("\n--- Editing plan manually ---\n")
                plan = _apply_set_params(plan, set_params)
            else:
                plan = reviewed
        else:
            plan = _apply_set_params(plan, set_params)
    else:
        plan = _apply_set_params(plan, set_params)

    plan_ref = save_plan_to_default_path(plan)

    while True:
        clarification = next_clarification(plan, ask_plan_ref=plan_ref)
        if clarification is None:
            break

        if clarification.kind == "confirm":
            # Preflight validation before execution confirmation
            # Note: Validation requires inventory to be completed first, which happens
            # during execution. During TUI planning, we try to validate if possible,
            # but skip if the asset registry doesn't exist yet.
            if plan.selected_stages:
                validation_attempted = _attempt_preflight_validation(plan)
                if validation_attempted:
                    # Validation found blocking issues
                    choice = _prompt_validation_action()

                    if choice == "modify_stages":
                        print("\n--- Restarting from stage selection ---\n")
                        plan = _reset_plan_selection(plan)
                        plan_ref = save_plan_to_default_path(plan)
                        continue
                    elif choice == "provide_data":
                        print("\n📝 Please add the missing assets to your manifest.")
                        raw = input("After updating the manifest, press Enter to re-validate, or type b to edit the plan: ").strip().lower()
                        if raw in {"b", "back", "edit"}:
                            print("\n--- Restarting from stage selection ---\n")
                            plan = _reset_plan_selection(plan)
                            plan_ref = save_plan_to_default_path(plan)
                            continue

                        # Re-validate after user updates manifest
                        print("\n--- Re-validating after manifest update ---\n")
                        validation_attempted = _attempt_preflight_validation(plan)
                        if validation_attempted:
                            # Still has blocking issues - reset to stage selection
                            print("\n⚠️  Validation still failed. Returning to stage selection so you can edit the plan.\n")
                            plan = _reset_plan_selection(plan)
                            plan_ref = save_plan_to_default_path(plan)
                            continue
                        else:
                            # Validation passed - continue to confirmation
                            print("✓ Validation passed! Continuing...\n")
                    elif choice == "proceed_anyway":
                        print("\n⚠️  Warning: continuing despite preflight validation errors.")
                        print("Execution may fail later or stop at a human-review gate if required inputs are still missing.")

            _render_confirm(clarification)
            if not _prompt_confirm("Proceed?"):
                print("\n--- Restarting from stage selection ---\n")
                plan = _reset_plan_selection(plan)
                plan_ref = save_plan_to_default_path(plan)
                continue
            response = AskClarificationResponse(kind="confirm", confirmed=True)
        else:
            response = _prompt_for_answer(clarification, plan)

        plan = merge_clarification_response(plan, response)
        plan_ref = save_plan_to_default_path(plan)

    return _execute_confirmed_plan(plan, args)


def apply_set_params(plan: AskPlanState, set_params: list[str]) -> AskPlanState:
    """Apply --set params to a plan. Public for use in non-interactive mode."""
    return _apply_set_params(plan, set_params)


def _parse_set_params(raw: list[str]) -> dict[str, str]:
    """Parse --set KEY=VALUE arguments into a dictionary (internal helper)."""
    result: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--set value must be KEY=VALUE, got: {item!r}")
        key, _, value = item.partition("=")
        result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "executable": "run",
    "dependency": "dep",
}

_PARAM_HINTS: dict[str, str] = {
    "assembly_review.eval.busco.lineage_path": "path to BUSCO lineage dir, e.g. /db/busco/insecta_odb10",
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
    "hic_scaffolding.candidate.yahs.hic_alignment_ref": "materialized Hi-C alignment BAM for YaHS",
    "hic_scaffolding.candidate.juicer_3ddna_juicebox.chromosome_count": "haploid chromosome count, e.g. 22 for 2n=44",
    "hic_scaffolding.candidate.juicer_3ddna_juicebox.restriction_enzyme": "Hi-C restriction enzyme, e.g. MboI or DpnII",
    "hic_scaffolding.candidate.juicer_3ddna_juicebox.chrom_sizes_ref": "chrom.sizes path; use auto to derive from the pre-Hi-C FASTA",
    "repeat_annotation.masking.repeatmasker.threads": "CPU threads for RepeatMasker",
    "read_qc.length_filter.seqkit_seq.min_len": "minimum read length to keep (bp)",
    "t2t.alignment.minimap2.mode": "auto_patch / manual_review / propose_only",
    "t2t.alignment.minimap2.telomere_motif": "terminal repeat motif, e.g. TTAGGG",
    "t2t.alignment.minimap2.threshold_min_motif_copies": "minimum motif copies at a read/contig end",
    "t2t.alignment.minimap2.threshold_terminal_window_bp": "terminal window scanned for telomere signal",
    "t2t.alignment.minimap2.threshold_min_mapq": "minimum minimap2 mapping quality for candidate reads",
    "t2t.alignment.minimap2.threshold_max_target_terminal_distance_bp": "maximum distance from contig end",
    "t2t.alignment.minimap2.threshold_min_overlap_bp": "minimum read-to-assembly overlap",
    "t2t.alignment.minimap2.threshold_min_supporting_reads": "compatible telomeric reads required before patching",
}

_GENOME_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.IGNORECASE)
_GENOME_SIZE_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
_SHARED_PLATFORM_VALUES = ("cycloneseq", "ont", "clr", "pacbio_hifi", "illumina", "unknown")
_SHARED_READ_TYPE_VALUES = tuple(SHARED_TO_METHOD_READ_TYPE.keys())

_STAGE_DESCRIPTIONS: dict[str, str] = {
    "read_qc": "quality stats and optional length filtering",
    "genome_survey": "k-mer profiling and genome size estimation",
    "assembly": "de novo genome assembly",
    "assembly_review": "QUAST/BUSCO quality evaluation",
    "hic_scaffolding": "Hi-C based chromosome scaffolding",
    "repeat_annotation": "repeat element identification and masking",
    "gene_annotation": "gene structure prediction",
    "function_annotation": "protein function annotation",
}


def _render_confirm(req: AskClarificationRequest) -> None:
    _render_plan_summary(req.summary or {}, title="Run Plan")


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
            print("\nDraft locked. Next, review the final run plan before execution starts.")
            return plan
        if action == "restart":
            return None
        if action == "stages":
            plan = _edit_draft_stages(plan)
            continue
        if action.startswith("stage:"):
            _, stage = action.split(":", 1)
            plan = _edit_draft_stage(plan, stage)
            continue
        if action.startswith("method:"):
            _, stage = action.split(":", 1)
            plan = _edit_stage_methods(plan, stage)
            continue
        if action.startswith("params:"):
            _, stage = action.split(":", 1)
            plan = _edit_stage_parameters(plan, stage)
            continue
        print("Unknown action. Press Enter to continue, a stage number to edit, s for stages, or r to restart.")


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
    print("  - A final run plan will be shown before execution.")
    print("  - You can edit a stage here first, then return to this plan.")

    attention_items = _draft_attention_items(plan, summary)
    order = summary.get("resolved_execution_order") or []
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

    stages_needing_attention = [stage for stage in order if _draft_stage_attention(stage, plan, summary, attention_items)]
    if stages_needing_attention:
        print("\nNeeds attention:")
        for stage in stages_needing_attention:
            print(f"  - {stage}")

    print("\nActions:")
    print("  Enter / c   continue")
    for idx, stage in enumerate(order, start=1):
        print(f"  {idx:<10} edit {stage}")
    print("  s           edit stage list")
    print("  r           restart manually")
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
            details = [str(value) for value in (platform, fmt) if value and value != "unknown"]
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
) -> None:
    print(f"\n{idx}. {stage}")
    print(f"   status: {_draft_stage_status(stage, plan, summary)}")
    method_lines = _draft_stage_method_lines(stage, plan, summary)
    if method_lines:
        print("   methods:")
        for line in method_lines:
            print(f"     {line}")
    parameter_lines = _draft_stage_parameter_lines(stage, plan, summary)
    if parameter_lines:
        print("   parameters:")
        for line in parameter_lines:
            print(f"     {line}")
    if attention:
        print("   attention:")
        for item in attention:
            print(f"     - {item}")
    if nested:
        print("   edit: m method / p parameters / b back")
    else:
        print(f"   edit: {idx} / {idx}m method / {idx}p parameters")


def _draft_stage_status(stage: str, plan: AskPlanState, summary: dict[str, Any]) -> str:
    roles = summary.get("stage_roles") or {}
    role = roles.get(stage)
    selected = _selected_methods_for_stage(plan, stage)
    if stage in _STAGES_NEEDING_METHOD_SELECTION and not selected:
        return "method needed before locking"
    if selected:
        return "will run locally"
    if role == "dependency":
        return "included as a dependency"
    return "included"


def _draft_stage_method_lines(stage: str, plan: AskPlanState, summary: dict[str, Any]) -> list[str]:
    selected = _selected_methods_for_stage(plan, stage)
    if not selected and stage in _STAGES_NEEDING_METHOD_SELECTION:
        return ["missing"]
    return list(selected)


def _draft_stage_parameter_lines(stage: str, plan: AskPlanState, summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    missing = set(summary.get("missing_parameter_fields") or [])
    shared_stage = _shared_parameter_stage(plan)
    if stage == shared_stage:
        shared_values = {}
        shared_values.update(summary.get("shared_parameter_defaults") or {})
        shared_values.update(summary.get("shared_parameters") or {})
        shared_sources = summary.get("shared_parameter_default_sources") or {}
        for key, value in shared_values.items():
            field = f"shared_parameters.{key}"
            seen.add(field)
            note = _source_note(shared_sources.get(key), fallback="recommended")
            lines.append(f"{key}: {value}{note}")
        for field in sorted(item for item in missing if item.startswith("shared_parameters.")):
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
        for key, value in values.items():
            field = f"{method}.{key}"
            seen.add(field)
            note = _source_note(method_sources.get(key), fallback="recommended") if key in (defaults.get(method) or {}) else ""
            lines.append(f"[{short}] {key}: {value}{note}")
        for field in sorted(item for item in missing if item.startswith(f"{method}.")):
            if field not in seen:
                seen.add(field)
                lines.append(f"[{short}] {field.rsplit('.', 1)[-1]}: missing")

    stage_values = {}
    stage_values.update((summary.get("stage_parameter_defaults") or {}).get(stage) or {})
    stage_values.update((summary.get("stage_parameters") or {}).get(stage) or {})
    for key, value in stage_values.items():
        field = f"{stage}.{key}"
        seen.add(field)
        if isinstance(value, list):
            lines.append(f"{key}: {len(value)} record(s)")
        else:
            lines.append(f"{key}: {value}")
    for field in sorted(item for item in missing if _stage_for_field(item, plan) == stage):
        if field not in seen:
            lines.append(f"{field.rsplit('.', 1)[-1]}: missing")
    return lines


def _draft_stage_attention(
    stage: str,
    plan: AskPlanState,
    summary: dict[str, Any],
    attention_items: list[tuple[str, str]],
) -> list[str]:
    """Collect attention items for a specific stage. Filters inline instead of pre-aggregating."""
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


def _draft_attention_items(plan: AskPlanState, summary: dict[str, Any]) -> list[tuple[str, str]]:
    items = list(_draft_blocking_items(plan, summary))
    return list(dict.fromkeys(items))


def _draft_blocking_items(plan: AskPlanState, summary: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for stage in plan.resolved_execution_order or []:
        if stage in _STAGES_NEEDING_METHOD_SELECTION and not _selected_methods_for_stage(plan, stage):
            items.append((stage, "method is missing"))
    for field in summary.get("missing_parameter_fields") or []:
        stage = _stage_for_field(str(field), plan)
        items.append((stage, f"{_field_label(str(field))} is missing"))
    if summary.get("run_config_preview_error"):
        stage = (plan.resolved_execution_order or ["plan"])[0]
        items.append((stage, f"run config is not ready: {summary['run_config_preview_error']}"))
    items.extend(_next_clarification_blocking_items(plan, summary))
    return list(dict.fromkeys(items))


def _next_clarification_blocking_items(plan: AskPlanState, summary: dict[str, Any]) -> list[tuple[str, str]]:
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
    stages = list(plan.resolved_execution_order or [])
    if _HAS_QUESTIONARY:
        choices = [
            questionary.Choice("continue", value="continue"),
            *[questionary.Choice(f"edit {idx}. {stage}", value=f"stage:{stage}") for idx, stage in enumerate(stages, 1)],
            questionary.Choice("edit stage list", value="stages"),
            questionary.Choice("restart manually", value="restart"),
        ]
        return _safe_ask(questionary.select("Plan action:", choices=choices, default="continue"))

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
            return ("method:" if raw[-1] == "m" else "params:") + stages[idx]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(stages):
            return f"stage:{stages[idx]}"
    return raw


def _edit_draft_stage(plan: AskPlanState, stage: str) -> AskPlanState:
    current = plan
    invalid_attempts = 0
    while True:
        summary = build_run_plan_summary(current)
        print()
        print("-" * 64)
        print(stage)
        print("-" * 64)
        attention_items = _draft_attention_items(current, summary)
        attention = _draft_stage_attention(stage, current, summary, attention_items)
        _render_draft_stage_summary(1, stage, current, summary, attention, nested=True)
        print("\nActions:")
        print("  m      change method")
        print("  p      edit parameters")
        print("  b      back to plan")
        raw = input(f"{stage}> ").strip().lower()
        if raw in ("", "b", "back"):
            return current
        if raw in ("m", "method", "methods"):
            invalid_attempts = 0
            current = _edit_stage_methods(current, stage)
            continue
        if raw in ("p", "param", "params", "parameters"):
            invalid_attempts = 0
            current = _edit_stage_parameters(current, stage)
            continue
        invalid_attempts += 1
        print("Unknown action. Use m, p, or b.")
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
        choices={"available_stages": _all_selectable_stages(), "suggested_stages": plan.selected_stages or []},
        required_fields=["selected_stages"],
        response_schema={"kind": "stage_selection", "selected_stages": plan.selected_stages or []},
        next_action={},
    )
    response = _prompt_stage_selection(req)
    return _refresh_draft_defaults(merge_clarification_response(plan, response))


def _edit_stage_methods(plan: AskPlanState, stage: str) -> AskPlanState:
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
        response_schema={"kind": "method_selection", "selected_methods": {stage: default}},
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
            "defaults": _filter_method_map_for_stage(summary.get("parameter_defaults") or {}, plan, stage),
            "shared_defaults": summary.get("shared_parameter_defaults") or {},
            "stage_defaults": {stage: (summary.get("stage_parameter_defaults") or {}).get(stage) or {}},
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

    stage_keys = set(((summary.get("stage_parameter_defaults") or {}).get(stage) or {}).keys())
    stage_keys.update(((summary.get("stage_parameters") or {}).get(stage) or {}).keys())
    for field in summary.get("missing_parameter_fields") or []:
        field_text = str(field)
        method_scoped = any(field_text.startswith(f"{method}.") for method in _selected_methods_for_stage(plan, stage))
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
    if not selected:
        return []
    return selected if isinstance(selected, list) else [selected]


def _shared_parameter_stage(plan: AskPlanState) -> str | None:
    order = list(plan.resolved_execution_order or [])
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
    if field == FUNCTION_DATABASE_PROVENANCE_FIELD:
        return "function_annotation"
    for stage, methods in (plan.selected_methods or {}).items():
        method_list = methods if isinstance(methods, list) else [methods]
        for method in method_list:
            if field.startswith(f"{method}."):
                return stage
    stage, _, _ = field.partition(".")
    return stage or "plan"


def _stage_for_warning(warning: str, plan: AskPlanState) -> str:
    for stage in plan.resolved_execution_order or []:
        if warning.startswith(f"{stage}:") or f"{stage}." in warning:
            return stage
    return _shared_parameter_stage(plan) or "plan"


def _field_label(field: str) -> str:
    if field.startswith("shared_parameters."):
        return field.rsplit(".", 1)[-1]
    if field == FUNCTION_DATABASE_PROVENANCE_FIELD:
        return "database_provenance"
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
            "deterministic_fallback": "local deterministic fallback",
        }.get(str(parse_source), str(parse_source))
        print(f"Intent parser: {parse_label}")
    _render_summary_inputs(summary)

    roles = summary.get("stage_roles", {})
    order = summary.get("resolved_execution_order", [])
    if order:
        print("\nStages:")
        for stage in order:
            role = roles.get(stage, "")
            label = _ROLE_LABELS.get(role, role)
            print(f"  [{label:>9}] {stage}")

    methods = summary.get("selected_methods", {})
    method_note = "  (recommended)" if draft is not None else ""
    if preview and order:
        print("\nMethods:")
        for stage in order:
            value = methods.get(stage)
            if not value:
                print(f"  {stage}: pending")
                continue
            method_list = value if isinstance(value, list) else [value]
            for m in method_list:
                print(f"  {stage}: {m}{method_note}")
    elif methods:
        print("\nMethods:")
        for stage, value in methods.items():
            method_list = value if isinstance(value, list) else [value]
            for m in method_list:
                print(f"  {stage}: {m}")

    missing_fields = list(summary.get("missing_parameter_fields") or [])
    shared_defaults = summary.get("shared_parameter_defaults", {}) if preview else {}
    pending_shared = _pending_shared_parameter_names(missing_fields)
    deferred_shared = summary.get("deferred_shared_parameters", {}) if preview else {}
    shared_sources = summary.get("shared_parameter_default_sources", {}) if preview else {}
    shared = {**shared_defaults, **(summary.get("shared_parameters", {}) or {})}
    if shared or deferred_shared or (preview and pending_shared):
        print("\nShared parameters:")
        for k, v in shared.items():
            note = ""
            if draft is not None:
                note = "  (from request)" if k in explicit_shared else _source_note(shared_sources.get(k), fallback="recommended")
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
        _render_preview_parameters(summary, params, recommended_params=draft is not None)
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
    pending_by_method = _pending_method_parameter_names(missing_fields, set(method_order) | set(defaults) | set(params))
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
        for key, value in values.items():
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
    order = summary.get("resolved_execution_order", []) or list(methods)
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


def _group_missing_fields_by_scope(missing_fields: list[str], methods: set[str] | None = None) -> dict[str, Any]:
    """Parse missing_fields and group by scope: shared, stage, or method.

    Returns dict with keys:
        - shared: list[str] of shared parameter names (without 'shared_parameters.' prefix)
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
            shared.append(field[len("shared_parameters."):])
        elif methods:
            # Try to match against known methods
            matched = False
            for m in methods:
                prefix = f"{m}."
                if field.startswith(prefix):
                    method.setdefault(m, []).append(field[len(prefix):])
                    matched = True
                    break
            if not matched:
                # Fall back to stage-scoped
                parts = field.split(".")
                if len(parts) == 2:
                    stage.setdefault(parts[0], []).append(parts[1])
                else:
                    logger.debug("Dropping unclassifiable missing field %r (no matching method, not stage-scoped)", field)
        else:
            # No methods provided, assume stage-scoped
            parts = field.split(".")
            if len(parts) == 2:
                stage.setdefault(parts[0], []).append(parts[1])
            else:
                logger.debug("Dropping unclassifiable missing field %r (not stage-scoped)", field)

    return {"shared": shared, "stage": stage, "method": method}


def _pending_shared_parameter_names(missing_fields: list[str]) -> list[str]:
    """Extract shared parameter names from missing_fields."""
    return _group_missing_fields_by_scope(missing_fields)["shared"]


def _pending_stage_parameter_names(missing_fields: list[str]) -> dict[str, list[str]]:
    """Extract stage-scoped parameter names from missing_fields."""
    return _group_missing_fields_by_scope(missing_fields)["stage"]


def _pending_method_parameter_names(missing_fields: list[str], methods: set[str]) -> dict[str, list[str]]:
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
                    rendered = ", ".join(f"{key}={value}" for key, value in params.items())
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


def _prompt_for_answer(req: AskClarificationRequest, plan: AskPlanState) -> AskClarificationResponse:
    if req.kind == "stage_selection":
        return _prompt_stage_selection(req)
    if req.kind == "method_selection":
        return _prompt_method_selection(req)
    if req.kind == "parameter_values":
        return _prompt_parameter_values(req, plan)
    raise ValueError(f"unexpected clarification kind: {req.kind}")


def _prompt_confirm(message: str) -> bool:
    if _HAS_QUESTIONARY:
        return _safe_ask(questionary.confirm(message, default=False))
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
    suggested = req.choices.get("suggested_stages", [])

    if _HAS_QUESTIONARY:
        print("\n--- Step 1: Stage Selection ---")
        print("  Recommended stages are pre-checked based on your request/input data.\n")
        choices = [
            questionary.Choice(
                f"{stage} — {_STAGE_DESCRIPTIONS.get(stage, '')}",
                value=stage,
                checked=(stage in suggested),
            )
            for stage in available
        ]
        selected = _safe_ask(questionary.checkbox(
            "Select stages:",
            choices=choices,
            instruction="(Space toggle, Enter confirm)",
        ))
        if not selected:
            selected = suggested
        return AskClarificationResponse(kind="stage_selection", selected_stages=selected)

    # Plain-text fallback
    print("\n--- Step 1: Stage Selection ---")
    print("  * = recommended based on your request/input data\n")
    print("Available stages:")
    for i, stage in enumerate(available, 1):
        marker = "*" if stage in suggested else " "
        desc = _STAGE_DESCRIPTIONS.get(stage, "")
        desc_str = f"  {desc}" if desc else ""
        print(f"  {marker} {i}. {stage}{desc_str}")
    if suggested:
        print(f"\n  Recommended: {', '.join(suggested)}")
    print("\n  Enter numbers or names, e.g. 1,3 or read_qc,assembly")
    print("  Press Enter to accept recommended.")
    raw = input("> ").strip()
    stages = _parse_selection(raw, available) if raw else suggested
    return AskClarificationResponse(kind="stage_selection", selected_stages=stages)


def _prompt_method_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    methods_by_stage = req.choices.get("methods_by_stage", {})
    selected: dict[str, str | list[str]] = {}

    print("\n--- Step 2: Method Selection ---\n")

    for stage, methods in methods_by_stage.items():
        method_names = [m["method_name"] for m in methods]
        if stage == "read_qc":
            selected[stage] = _prompt_read_qc_filter_choice(methods, req.choices.get("defaults", {}).get(stage))
            continue
        requested_default = req.choices.get("defaults", {}).get(stage)
        if isinstance(requested_default, list):
            default_checked = [m for m in requested_default if m in method_names]
        elif requested_default:
            default_checked = [requested_default] if requested_default in method_names else []
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
                choices.append(questionary.Choice(
                    m["method_name"],
                    value=m["method_name"],
                    checked=(m["method_name"] in default_checked),
                ))
            if stage == "assembly":
                selected[stage] = _safe_ask(questionary.select(
                    f"Primary method for {stage}:",
                    choices=choices,
                    default=default_checked[0],
                    instruction="(choose one primary)",
                ))
            else:
                answer = _safe_ask(questionary.checkbox(
                    f"Methods for {stage}:",
                    choices=choices,
                    instruction="(Space toggle, Enter confirm)",
                ))
                if not answer:
                    answer = default_checked
                selected[stage] = answer[0] if len(answer) == 1 else answer
        else:
            print(f"  Methods for [{stage}]:")
            for i, m in enumerate(methods, 1):
                marker = "*" if m["method_name"] in default_checked else " "
                print(f"    {marker} {i}. {m['method_name']}")
            prompt = "Choose one number or name" if stage == "assembly" else "Enter numbers or names"
            print(f"\n  {prompt}. Enter = {', '.join(default_checked)}:")
            raw = input(f"  {stage}> ").strip()
            if not raw:
                picks = default_checked
            else:
                picks = _parse_selection(raw, method_names)
            selected[stage] = picks[0] if stage == "assembly" or len(picks) == 1 else picks

    return AskClarificationResponse(kind="method_selection", selected_methods=selected)


def _prompt_read_qc_filter_choice(methods: list[dict[str, Any]], default: str | list[str] | None) -> list[str]:
    default_methods = default if isinstance(default, list) else [default] if default else []
    default_filter = next((method for method in default_methods if method in READ_QC_FILTER_METHODS), READ_QC_FILTER_SKIP)
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
        answer = _safe_ask(questionary.select(
            "Read QC length filtering:",
            choices=choices,
            default=default_filter,
            instruction="(↑↓ navigate, Enter confirm)",
        ))
        filter_method = answer or default_filter
    else:
        print("  Methods for [read_qc]:")
        print(f"    * always: {READ_QC_STATS_METHOD}")
        print("    length filtering:")
        for i, m in enumerate(filters, 1):
            label = m.get("display_name") or m["method_name"]
            marker = "*" if m["method_name"] == default_filter else " "
            print(f"    {marker} {i}. {label}")
        print(f"    * always: {READ_QC_HANDOFF_METHOD}")
        print(f"\n  Choose one length filter. Enter = {default_filter}:")
        raw = input("  read_qc length_filter> ").strip()
        picks = _parse_selection(raw, filter_names) if raw else [default_filter]
        filter_method = picks[0] if picks else default_filter

    return [READ_QC_STATS_METHOD, filter_method, READ_QC_HANDOFF_METHOD]


def _prompt_parameter_values(req: AskClarificationRequest, plan: AskPlanState) -> AskClarificationResponse:
    defaults = req.choices.get("defaults", {})
    shared_defaults = dict(req.choices.get("shared_defaults", {}))
    stage_defaults = dict(req.choices.get("stage_defaults", {}))
    params: dict[str, dict[str, Any]] = {}
    stage_params: dict[str, dict[str, Any]] = {}
    shared_params: dict[str, Any] = {}
    print("\n--- Step 3: Parameters ---")
    print("  Press Enter to accept [default], or type a new value.\n")
    hic_method = "hic_scaffolding.candidate.juicer_3ddna_juicebox"
    handled = set()
    if hic_method in _selected_method_names(plan):
        params[hic_method] = _prompt_juicer_3ddna_parameters(dict(defaults.get(hic_method) or {}))
        handled = {field for field in req.required_fields if field.startswith(hic_method + ".")}
    for field in req.required_fields:
        if field in handled:
            continue
        method_key, _, param_name = field.rpartition(".")
        if not method_key:
            method_key = param_name
        short_method = method_key.rsplit(".", 1)[-1]
        stage_name = method_key.split(".")[0] if "." in method_key else method_key
        label = f"[{stage_name} / {short_method}] {param_name}"

        hint = _PARAM_HINTS.get(f"{method_key}.{param_name}") or _PARAM_HINTS.get(param_name)
        if hint:
            label = f"{label}  ({hint})"

        if method_key == "shared_parameters":
            current_shared_defaults = _derive_shared_parameter_defaults({**shared_defaults, **shared_params})
            default = current_shared_defaults.get(param_name)
            if param_name == "read_type" and default and current_shared_defaults.get("platform"):
                if not _shared_parameter_value_is_valid("read_type", default):
                    default = None
                else:
                    print(f"  [shared_parameters] read_type derived from platform={current_shared_defaults.get('platform')}: {default}")
                    shared_params[param_name] = default
                    continue
        else:
            default = _find_default(field, defaults)

        if method_key == "shared_parameters":
            value = _prompt_shared_parameter(label, param_name, default)
            shared_params[param_name] = value
            shared_params = _derive_shared_parameter_defaults(shared_params)
        elif field == "function_annotation.database_provenance":
            value = _prompt_function_database_provenance(plan)
            stage_params.setdefault("function_annotation", {})["database_provenance"] = value
        else:
            value = _prompt_text_value(label, default, required=True)
            if param_name == "estimated_genome_size" and isinstance(value, str):
                value = _parse_genome_size(value)
            params.setdefault(method_key, {})[param_name] = value
            _update_derived_defaults(method_key, param_name, value, defaults)
    return AskClarificationResponse(
        kind="parameter_values",
        shared_parameters=_derive_shared_parameter_defaults(shared_params) or None,
        method_parameters=params or None,
        stage_parameters=stage_params or None,
    )


def _prompt_juicer_3ddna_parameters(defaults: dict[str, Any]) -> dict[str, Any]:
    """Collect the mode-dependent minimum needed for a real Hi-C attempt."""
    mode = _prompt_text_value("[hic_scaffolding / juicer_3ddna_juicebox] input_mode (raw_reads or merged_nodups)", defaults.get("input_mode", "raw_reads"))
    result: dict[str, Any] = {
        "input_mode": mode,
        "chromosome_count": _prompt_text_value(
            "[hic_scaffolding / juicer_3ddna_juicebox] chromosome_count  (haploid chromosome count, e.g. 22 for 2n=44)",
            defaults.get("chromosome_count"),
        ),
    }
    if mode == "merged_nodups":
        result["merged_nodups_ref"] = _prompt_text_value(
            "[hic_scaffolding / juicer_3ddna_juicebox] merged_nodups_ref", defaults.get("merged_nodups_ref")
        )
    else:
        result["restriction_enzyme"] = _prompt_text_value(
            "[hic_scaffolding / juicer_3ddna_juicebox] restriction_enzyme  (Hi-C restriction enzyme, e.g. MboI or DpnII)",
            defaults.get("restriction_enzyme"),
        )
        result["chrom_sizes_ref"] = _prompt_text_value(
            "[hic_scaffolding / juicer_3ddna_juicebox] chrom_sizes_ref  (chrom.sizes path; Enter = auto from pre-Hi-C FASTA)",
            defaults.get("chrom_sizes_ref", "auto"),
        )
        result["threads"] = _prompt_text_value(
            "[hic_scaffolding / juicer_3ddna_juicebox] threads", defaults.get("threads", 8)
        )
    return result


def _selected_method_names(plan: AskPlanState) -> set[str]:
    names: set[str] = set()
    for value in (plan.selected_methods or {}).values():
        names.update(value if isinstance(value, list) else [value])
    return names


def _prompt_function_database_provenance(plan: AskPlanState) -> list[dict[str, Any]]:
    """Prompt for function annotation database provenance interactively.

    Asks simple per-method questions (database path, name, version) and
    auto-fills the rest (method_name, snapshot_date, descriptor_version).
    input_proteins_ref is left unset: function_annotation adopts the resolved
    proteins_fasta_ref at runtime, which the ask layer cannot know for
    manifest-provided proteins.  Users never see or construct JSON.
    """
    from datetime import date

    methods = (plan.selected_methods or {}).get("function_annotation")
    method_list = methods if isinstance(methods, list) else [methods] if methods else []

    _METHOD_LABELS = {
        "function_annotation.candidate.diamond": "DIAMOND",
    }

    records: list[dict[str, Any]] = []
    for method in method_list:
        label = _METHOD_LABELS.get(method, method.rsplit(".", 1)[-1])
        print(f"\n  -- {label} database provenance --")

        # Database source path (the only thing the user must provide)
        db_path = None
        invalid_attempts = 0
        while not db_path:
            raw = _prompt_text_value(f"{label} database path", None, required=True)
            candidate = Path(str(raw).strip()).expanduser()
            if candidate.exists():
                db_path = str(candidate)
                break
            invalid_attempts += 1
            print(f"    Database path does not exist for {label}: {candidate}")
            if invalid_attempts >= _MAX_INVALID_PROMPT_ATTEMPTS:
                raise _UserAbort()

        # Database name (optional, defaults to label)
        db_name = _prompt_text_value(f"{label} database name", label)
        if db_name is None:
            db_name = label

        # Database version (optional, defaults to "current")
        db_version = _prompt_text_value(f"{label} database version", "current")
        if db_version is None:
            db_version = "current"

        record = {
            "method_name": method,
            "database_name": str(db_name),
            "database_version": str(db_version),
            "snapshot_date": date.today().isoformat(),
            "database_source_ref": db_path,
            "descriptor_version": f"{method.rsplit('.', 1)[-1]}.yaml",
        }
        records.append(FunctionDatabaseProvenance.model_validate(record).model_dump(mode="json"))

    if not records:
        return records

    print(f"\n  database_provenance: {len(records)} record(s) configured.")
    return records


def _prompt_text_value(label: str, default: Any, *, required: bool = False) -> Any:
    invalid_attempts = 0
    while True:
        if _HAS_QUESTIONARY:
            answer = _safe_ask(questionary.text(
                f"{label}:",
                default=str(default) if default is not None else "",
            ))
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
        return _safe_ask(questionary.select(
            f"{label}:",
            choices=list(_SHARED_PLATFORM_VALUES),
            default=default_value if default_value in _SHARED_PLATFORM_VALUES else "unknown",
            instruction="(used for asset registry platform)",
        ))

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
        return _safe_ask(questionary.select(
            f"{label}:",
            choices=[
                questionary.Choice(_read_type_choice_label(value), value=value)
                for value in _SHARED_READ_TYPE_VALUES
            ],
            default=default_value if default_value in _SHARED_READ_TYPE_VALUES else None,
            instruction="(use normalized shared read type)",
        ))

    alias_text = ", ".join(f"{alias}->{value}" for alias, value in sorted(FLYE_TO_SHARED_READ_TYPE.items()))
    print(f"    accepted: {', '.join(_SHARED_READ_TYPE_VALUES)}")
    print(f"    Flye aliases accepted: {alias_text}")
    while True:
        value = _prompt_text_value(label, default_value)
        normalized = _normalize_shared_read_type(value)
        if _shared_parameter_value_is_valid("read_type", normalized):
            return normalized


def _read_type_choice_label(value: str) -> str:
    flye_aliases = sorted(alias for alias, normalized in FLYE_TO_SHARED_READ_TYPE.items() if normalized == value)
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


def _update_derived_defaults(method_key: str, param_name: str, value: Any, defaults: dict[str, Any]) -> None:
    """After user provides a value, derive related defaults (e.g. BUSCO lineage fields from path)."""
    if method_key == "assembly_review.eval.busco" and param_name == "lineage_path" and value:
        from pathlib import Path as _Path
        name = _Path(str(value)).name
        bucket = defaults.setdefault(method_key, {})
        bucket.setdefault("lineage_name", name)
        m = re.search(r"(odb\d+)", name)
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


def _apply_set_params(plan: AskPlanState, set_params: list[str]) -> AskPlanState:
    if not set_params:
        return plan
    parsed = _parse_set_params(set_params)
    # Assembly/Flye MVP only. Generalize via method descriptors when more
    # executable methods are supported.
    _PARAM_ALIASES = {
        "genome_size": ("shared_parameters", "estimated_genome_size"),
        "estimated_genome_size": ("shared_parameters", "estimated_genome_size"),
        "platform": ("shared_parameters", "platform"),
        "read_type": ("shared_parameters", "read_type"),
        "threads": ("assembly.candidate.flye", "threads"),
        "hic_input_mode": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "input_mode"),
        "hic_chromosome_count": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "chromosome_count"),
        "hic_restriction_enzyme": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "restriction_enzyme"),
        "hic_chrom_sizes_ref": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "chrom_sizes_ref"),
        "hic_threads": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "threads"),
        "hic_merged_nodups_ref": ("hic_scaffolding.candidate.juicer_3ddna_juicebox", "merged_nodups_ref"),
    }
    existing = dict(plan.method_parameters or {})
    shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
    for key, value in parsed.items():
        alias = _PARAM_ALIASES.get(key)
        if alias:
            method, param = alias
            if method == "shared_parameters":
                if param == "estimated_genome_size":
                    value = _parse_genome_size(value)
                if param == "read_type":
                    value = FLYE_TO_SHARED_READ_TYPE.get(str(value), value)
                if param == "platform":
                    value = _normalize_shared_platform(value)
                shared[param] = value
                continue
            bucket = dict(existing.get(method, {}))
            bucket[param] = value
            existing[method] = bucket
    update = {}
    if existing:
        update["method_parameters"] = existing
    if shared:
        shared = _derive_shared_parameter_defaults(shared)
        update["shared_parameters"] = SharedParameters.model_validate(shared)
    if update:
        return plan.model_copy(update=update)
    return plan


def _attempt_preflight_validation(plan: AskPlanState) -> bool:
    """
    Attempt preflight validation before execution.

    Returns True if validation failed or could not be completed safely.
    Returns False if validation passed.
    """
    try:
        # Load manifest and run inventory to get asset registry
        from app.stages.inventory.nodes import register_assets, validate_inputs
        from app.schemas.state import initial_state
        from app.stages.preflight import validate_execution_plan

        manifest_path = Path(plan.manifest_ref)
        if not manifest_path.exists():
            print(f"\nPreflight validation could not run: manifest not found: {manifest_path}", file=sys.stderr)
            return True

        # Create initial state and run inventory to get asset_registry_ref
        state = initial_state(
            session_id=plan.session_id,
            input_manifest_ref=plan.manifest_ref,
            data_dir=plan.data_dir,
        )

        # Run the inventory nodes needed to populate asset_registry_ref.
        for inventory_step in (validate_inputs, register_assets):
            update = inventory_step(state)
            if update:
                state = {**state, **update}

        # Now validate with the populated state
        validation = validate_execution_plan(
            state=state,
            requested_stages=plan.selected_stages or [],
        )

        if not validation.valid:
            _render_validation_errors(validation)
            return True

        return False

    except Exception as exc:
        logger.warning("Preflight validation failed unexpectedly.", exc_info=True)
        print(
            f"\nPreflight validation could not complete: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print("Fix the validation error, edit the plan, or choose the advanced override to proceed anyway.", file=sys.stderr)
        return True


def _render_validation_errors(validation) -> None:
    """Display friendly validation errors to the user."""
    print(f"\n{validation.summary}\n")

    # Display first blocker details
    if validation.first_blocker:
        blocker = validation.checks[validation.first_blocker]
        print(f"Stage: {blocker.stage}")
        print(f"Status: {'✓ can run' if blocker.can_run else '✗ cannot run'}\n")

        if blocker.missing:
            print("Missing resources:")
            for res in blocker.missing:
                print(f"  • {res.resource_name}: {res.message}")

        if blocker.suggestions:
            print("\n💡 Suggestions:")
            for suggestion in blocker.suggestions:
                print(f"  → {suggestion}")

    # Display overall suggestions
    if validation.suggestions:
        print("\n📋 Next steps:")
        for suggestion in validation.suggestions:
            print(f"  • {suggestion}")


def _prompt_validation_action() -> str:
    """Prompt user for action when validation fails."""
    print("\nWhat would you like to do?")
    print("  1. Modify stage selection (remove blocked stages)")
    print("  2. Provide missing data (add assets to manifest)")
    print("  3. Proceed anyway (advanced)")

    invalid_attempts = 0
    while True:
        choice = input("\nYour choice [1/2/3]: ").strip()
        if choice == "1":
            return "modify_stages"
        elif choice == "2":
            return "provide_data"
        elif choice == "3":
            return "proceed_anyway"
        else:
            invalid_attempts += 1
            print("Invalid choice. Please enter 1, 2, or 3.")
            if invalid_attempts >= _MAX_INVALID_PROMPT_ATTEMPTS:
                raise _UserAbort()


def _execute_confirmed_plan(plan: AskPlanState, args: argparse.Namespace) -> int:
    from app.entrypoints.ask_execution import execute_confirmed_plan
    return execute_confirmed_plan(plan, args)
