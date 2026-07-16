"""Staged planning helpers for `gene-agent ask`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from pydantic import ValidationError

from app.schemas.ask_plan import (
    AskClarificationRequest,
    AskClarificationResponse,
    AskPlanState,
    save_ask_plan,
)
from app.schemas.manifest import InputManifest
from app.schemas.run_config import AutoHiCRunParameters, RunConfig
from app.schemas.shared_parameters import FLYE_TO_SHARED_READ_TYPE, PLATFORM_TO_SHARED_READ_TYPE, SharedParameters
from app.schemas.warnings import PlanWarning as PlanWarning
from app.review.gates import planned_review_gates
from app.stages import STAGE_ORDER
from app.stages.genome_survey.constants import GENOME_SURVEY_EXECUTABLE_METHODS

from .planner_catalog import (
    build_planner_method_catalog as build_planner_method_catalog,
    load_planner_method_catalog,
)

FUNCTION_DATABASE_PROVENANCE_FIELD = "function_annotation.database_provenance"
FUNCTION_ANNOTATION_DIAMOND_METHOD = "function_annotation.candidate.diamond"


_ASSEMBLY_METHODS_NEED_SHARED_GENOME_SIZE = {
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
}
_ASSEMBLY_METHODS_NEED_SHARED_READ_TYPE = {
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
    "assembly.candidate.hifiasm",
    "assembly.candidate.hifiasm_ont",
}

# ---------------------------------------------------------------------------
# Planner stage-selection policy. Method metadata comes from descriptors.
# ---------------------------------------------------------------------------

_STAGE_SELECTION_MODE: dict[str, str] = {
    "read_qc": "read_qc_filter_choice",
    "genome_survey": "choose_many",
    "assembly": "choose_many",
    "assembly_review": "choose_many",
    "hic_scaffolding": "pipeline",
    "t2t": "choose_many",
    "repeat_annotation": "choose_many",
    "gene_annotation": "choose_many",
    "function_annotation": "choose_many",
}

READ_QC_STATS_METHOD = "read_qc.long_read_stats.seqkit_stats"
READ_QC_FILTER_SKIP = "read_qc.length_filter.skip"
READ_QC_FILTER_SEQKIT = "read_qc.length_filter.seqkit_seq"
READ_QC_HANDOFF_METHOD = "read_qc.make_fofn.local"
READ_QC_FILTER_METHODS = [
    READ_QC_FILTER_SKIP,
    READ_QC_FILTER_SEQKIT,
]
READ_QC_DEFAULT_METHODS = [READ_QC_STATS_METHOD, READ_QC_FILTER_SKIP, READ_QC_HANDOFF_METHOD]

def _build_assembly_review_config(plan: AskPlanState) -> dict[str, Any]:
    ar_config: dict[str, Any] = {}
    for descriptor in _executable_methods_for_stage("assembly_review"):
        method_name = descriptor["method_name"]
        params = (plan.method_parameters or {}).get(method_name)
        if not params:
            continue
        config_key = method_name.rsplit(".", 1)[-1]
        required = descriptor.get("required_parameters") or []
        defaults = descriptor.get("parameter_defaults") or {}
        block: dict[str, Any] = {}
        for field in required:
            value = params.get(field, defaults.get(field))
            if value is None:
                raise ValueError(f"assembly_review.eval.{config_key}.{field} is required")
            block[field] = value
        ar_config[config_key] = block
    return ar_config


_STAGES_NEEDING_METHOD_SELECTION = {
    stage for stage, mode in _STAGE_SELECTION_MODE.items() if mode != "pipeline"
}

def _get_method_descriptor(method_name: str) -> dict[str, Any] | None:
    return load_planner_method_catalog().descriptor_for_method(method_name)


def _all_known_methods() -> set[str]:
    return set(load_planner_method_catalog().by_method)


def _executable_methods_for_stage(stage: str) -> list[dict[str, Any]]:
    return load_planner_method_catalog().methods_for_stage(stage)


def _auto_fill_pipeline_methods(execution_order: list[str]) -> dict[str, str | list[str]]:
    """Auto-fill methods for pipeline stages (all executable methods run)."""
    result: dict[str, str | list[str]] = {}
    for stage in execution_order:
        if _STAGE_SELECTION_MODE.get(stage) == "pipeline":
            methods = [m["method_name"] for m in _executable_methods_for_stage(stage)]
            if methods:
                result[stage] = methods
    return result


def _default_methods_for_stage(stage: str) -> str | list[str] | None:
    if stage == "read_qc":
        return list(READ_QC_DEFAULT_METHODS)
    if stage == "hic_scaffolding":
        return ["hic_scaffolding.candidate.autohic"]
    if stage == "repeat_annotation":
        return ["repeat_annotation.masking.repeatmasker"]
    if stage == "gene_annotation":
        return ["gene_annotation.candidate.miniprot", "gene_annotation.candidate.augustus"]
    if stage == "function_annotation":
        return ["function_annotation.candidate.diamond"]
    if _STAGE_SELECTION_MODE.get(stage) == "pipeline":
        methods = [m["method_name"] for m in _executable_methods_for_stage(stage)]
        return methods or None
    return None


def ask_plan_path(data_dir: str | Path, session_id: str) -> Path:
    from app.storage.paths import SessionPaths

    return SessionPaths(data_dir, session_id).ask_plan_path


def locate_ask_plan_path(data_dir: str | Path, session_id: str) -> Path:
    """Return the sole v2 location for a persisted clarification plan."""

    return ask_plan_path(data_dir, session_id)


def save_plan_to_default_path(plan: AskPlanState) -> Path:
    return save_ask_plan(plan, ask_plan_path(plan.data_dir, plan.session_id))


def build_initial_plan_draft(plan: AskPlanState) -> AskPlanState | None:
    """Build an evidence-prefilled draft plan from the parsed workflow goal.

    Returns an unconfirmed candidate AskPlanState with target stages,
    execution order, safe default methods, and inferred parameters
    prefilled. Returns None when no workflow goal was parsed, so the caller
    falls back to manual stage selection.

    Contract: the result is a *candidate*, not committed state. The fields it
    reuses (selected_stages/selected_methods/...) carry recommended values, not
    operator-confirmed ones. The caller must show it for approval and only treat
    it as the live plan (persist it / feed it to next_clarification) once the
    operator accepts; on reject it must be discarded, never saved. A dedicated
    AskPlanDraft model was deliberately not introduced — see the data-boundary
    note in docs/phases/phase7/10_natural_language_draft_plan_approval.md.
    """
    target = _intent_requested_stages(plan)
    if not target:
        return None
    draft = merge_clarification_response(
        plan,
        AskClarificationResponse(kind="stage_selection", selected_stages=target),
    )
    draft = _fill_draft_default_methods(draft)
    draft = _auto_fill_inferred_defaults(draft)
    return draft


def _fill_draft_default_methods(plan: AskPlanState) -> AskPlanState:
    """Prefill conventional default methods for the draft preview.

    The draft should show the operator what the plan will become if they keep
    accepting recommended choices. Prefer executable local defaults where the
    interactive flow would do the same.
    """
    filled = _fill_missing_method_defaults(plan, dict(plan.selected_methods or {}))
    for stage in plan.resolved_execution_order or []:
        if stage in filled or stage not in _STAGES_NEEDING_METHOD_SELECTION:
            continue
        default = _draft_default_methods_for_stage(plan, stage)
        if default:
            filled[stage] = default
    if filled == (plan.selected_methods or {}):
        return plan
    return plan.model_copy(update={"selected_methods": _normalize_selected_methods(filled) or None})


def _draft_default_methods_for_stage(plan: AskPlanState, stage: str) -> str | list[str] | None:
    default = _default_methods_for_stage(stage)
    if default:
        return default
    if stage == "genome_survey":
        return ["genome_survey.kmer_count.jellyfish", "genome_survey.model.genomescope2"]
    if stage == "assembly":
        return _draft_assembly_method(plan)
    if stage == "assembly_review":
        return ["assembly_review.eval.quast"]
    if stage == "t2t":
        return "t2t.alignment.minimap2"
    executable = [method["method_name"] for method in _executable_methods_for_stage(stage)]
    return executable[0] if len(executable) == 1 else executable or None


def _draft_assembly_method(plan: AskPlanState) -> str | None:
    manifest = _load_manifest(plan.manifest_ref)
    if not manifest:
        return None
    asset_types = {asset.asset_type for asset in manifest.assets}
    if "raw_long_reads" not in asset_types:
        return None
    if "assembly_fasta_registered" in asset_types:
        return None
    platforms = _raw_long_read_platforms(manifest, include_unknown=True)
    if platforms == {"pacbio_hifi"}:
        return "assembly.candidate.hifiasm"
    return "assembly.candidate.flye"


def next_clarification(plan: AskPlanState, *, ask_plan_ref: str | Path) -> AskClarificationRequest | None:
    ref = str(Path(str(ask_plan_ref)).expanduser().resolve(strict=False))
    if plan.selected_stages is None:
        return _stage_selection_request(plan, ref)
    if plan.resolved_execution_order is None:
        return _stage_selection_request(plan, ref)
    if _needs_method_selection(plan):
        return _method_selection_request(plan, ref)
    if _needs_parameter_values(plan):
        return _parameter_values_request(plan, ref)
    plan = _auto_fill_inferred_defaults(plan)
    if not plan.confirmed:
        return _confirm_request(plan, ref)
    return None


def merge_clarification_response(plan: AskPlanState, response: AskClarificationResponse) -> AskPlanState:
    if response.kind == "stage_selection":
        stages = list(response.selected_stages or [])
        order = _resolve_execution_order(stages)
        pipeline_methods = _auto_fill_pipeline_methods(order)
        return plan.model_copy(update={
            "selected_stages": stages,
            "resolved_execution_order": order,
            "selected_methods": pipeline_methods or None,
            "method_parameters": None,
            "stage_parameters": None,
            "confirmed": False,
        })
    if response.kind == "method_selection":
        methods = _fill_missing_method_defaults(plan, dict(response.selected_methods or {}))
        existing = dict(plan.selected_methods or {})
        existing.update(methods)
        _validate_selected_methods(existing, plan)
        existing = _normalize_selected_methods(existing)
        return plan.model_copy(update={
            "selected_methods": existing,
            "method_parameters": None,
            "stage_parameters": None,
            "confirmed": False,
        })
    if response.kind == "parameter_values":
        existing = dict(plan.method_parameters or {})
        existing.update(response.method_parameters or {})
        update: dict[str, Any] = {"method_parameters": existing or None, "confirmed": False}
        stage_existing = dict(plan.stage_parameters or {})
        for stage, values in (response.stage_parameters or {}).items():
            merged = dict(stage_existing.get(stage) or {})
            merged.update(values)
            stage_existing[stage] = merged
        if stage_existing:
            update["stage_parameters"] = stage_existing
        if response.shared_parameters:
            current_shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
            current_shared.update(response.shared_parameters.model_dump(exclude_none=True))
            current_shared = _derive_shared_read_type_from_platform(current_shared)
            update["shared_parameters"] = SharedParameters.model_validate(current_shared)
        return plan.model_copy(update=update)
    if response.kind == "confirm":
        return plan.model_copy(update={"confirmed": bool(response.confirmed)})
    return plan


def build_run_config_payload(plan: AskPlanState) -> dict[str, Any]:
    local_stages = []
    for stage in plan.resolved_execution_order or []:
        if _stage_has_method(plan, stage):
            local_stages.append(stage)

    payload: dict[str, Any] = {"local_exec": local_stages}
    if plan.resolved_execution_order:
        payload["requested_stages"] = plan.resolved_execution_order
    if plan.shared_parameters:
        payload["shared_parameters"] = plan.shared_parameters.model_dump(exclude_none=True)
    assembly_config = _build_assembly_execution_config(plan, local_stages)
    if assembly_config:
        payload["assembly"] = assembly_config
    if "read_qc" in local_stages:
        payload["read_qc"] = {"length_filter": _read_qc_length_filter_config(plan)}
    if "genome_survey" in local_stages:
        survey_config = _build_genome_survey_config(plan)
        if survey_config:
            payload["genome_survey"] = survey_config
    if "assembly_review" in local_stages:
        ar_config = _build_assembly_review_config(plan)
        if ar_config:
            payload["assembly_review"] = ar_config
    if "hic_scaffolding" in local_stages:
        hic_config = _build_hic_scaffolding_config(plan)
        if hic_config:
            payload["hic_scaffolding"] = hic_config
        else:
            payload["local_exec"].remove("hic_scaffolding")
    if "t2t" in local_stages:
        payload["t2t"] = _build_t2t_config(plan)
    if "function_annotation" in local_stages:
        fn_config = _build_function_annotation_config(plan)
        if fn_config:
            payload["function_annotation"] = fn_config
    RunConfig.model_validate(payload)
    return payload


def _build_assembly_execution_config(
    plan: AskPlanState,
    local_stages: list[str],
) -> dict[str, Any] | None:
    """Translate the one executable assembly selection into the run-config API."""
    if "assembly" not in local_stages:
        return None

    selected = (plan.selected_methods or {}).get("assembly")
    methods = selected if isinstance(selected, list) else [selected] if selected else []
    executable = list(methods)
    if len(executable) != 1:
        raise ValueError(
            "assembly local execution requires exactly one executable method selection"
        )

    method_name = executable[0]
    config_method = method_name.rsplit(".", 1)[-1]
    params = dict((plan.method_parameters or {}).get(method_name) or {})
    if plan.shared_parameters:
        from app.schemas.shared_parameters import SHARED_TO_METHOD_READ_TYPE

        shared = plan.shared_parameters
        method_read_type = SHARED_TO_METHOD_READ_TYPE.get(shared.read_type or "", {}).get(config_method)
        if method_read_type:
            params.setdefault("read_type", method_read_type)
        if shared.estimated_genome_size:
            if config_method == "flye":
                params.setdefault("estimated_genome_size", shared.estimated_genome_size)
            elif config_method == "nextdenovo":
                params.setdefault("genome_size", shared.estimated_genome_size)

    parameter_key = f"{config_method}_parameters"
    config: dict[str, Any] = {"assembly_method": config_method, parameter_key: params}
    if config_method == "hifiasm":
        config["assembly_route"] = "hifi"
    elif config_method == "hifiasm_ont":
        config["assembly_route"] = "ont"
    return config


def _build_hic_scaffolding_config(plan: AskPlanState) -> dict[str, Any] | None:
    """Build AutoHiC runtime defaults; the stage LLM supplies the enzyme."""

    method = "hic_scaffolding.candidate.autohic"
    params = dict((plan.method_parameters or {}).get(method) or {})
    # Older ask-plan drafts stored model paths as method parameters. Models
    # are now resolved from packages.autohic.models in config/envs.yaml.
    params.pop("error_model_ref", None)
    params.pop("chromosome_model_ref", None)
    validated = AutoHiCRunParameters.model_validate(params)
    payload = validated.model_dump(exclude_none=True)
    return {"autohic_parameters": payload} if payload else {}


def write_ask_run_config(plan: AskPlanState) -> Path:
    plan = _auto_fill_inferred_defaults(plan)
    payload = build_run_config_payload(plan)
    from app.storage.paths import SessionPaths

    path = SessionPaths(plan.data_dir, plan.session_id).run_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clarification_template(request: AskClarificationRequest) -> dict[str, Any]:
    if request.kind == "stage_selection":
        return {"kind": "stage_selection", "selected_stages": request.choices.get("suggested_stages", [])}
    if request.kind == "method_selection":
        methods_by_stage = request.choices.get("methods_by_stage", {})
        selected = {}
        for stage, methods in methods_by_stage.items():
            default = request.choices.get("defaults", {}).get(stage)
            if default:
                selected[stage] = default
                continue
            executable = [m["method_name"] for m in methods]
            selected[stage] = executable[0] if executable else ""
        return {"kind": "method_selection", "selected_methods": selected}
    if request.kind == "parameter_values":
        defaults = dict(request.choices.get("defaults", {}))
        return {
            "kind": "parameter_values",
            "shared_parameters": dict(request.choices.get("shared_defaults", {})),
            "method_parameters": defaults,
            "stage_parameters": dict(request.choices.get("stage_defaults", {})),
        }
    return {"kind": "confirm", "confirmed": True}


def _stage_selection_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    available = _all_selectable_stages()
    suggested = _suggested_stages(plan, available)
    return AskClarificationRequest(
        status="clarification_required",
        kind="stage_selection",
        session_id=plan.session_id,
        ask_plan_ref=ask_plan_ref,
        manifest_ref=plan.manifest_ref,
        question="Which workflow stages should run?",
        choices={"available_stages": available, "suggested_stages": suggested},
        required_fields=["selected_stages"],
        response_schema={"kind": "stage_selection", "selected_stages": suggested},
        next_action=_next_action(ask_plan_ref),
    )


def _method_selection_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    methods_by_stage: dict[str, list[dict[str, Any]]] = {}
    defaults: dict[str, str | list[str]] = {}
    for stage in plan.resolved_execution_order or []:
        if stage not in _STAGES_NEEDING_METHOD_SELECTION:
            continue
        stage_methods = _executable_methods_for_stage(stage)
        if stage_methods:
            methods_by_stage[stage] = [
                {
                    "method_name": m["method_name"],
                    "selection_role": m.get("selection_role"),
                    "display_name": m.get("display_name"),
                }
                for m in stage_methods
            ]
            default = _default_methods_for_stage(stage)
            if default:
                defaults[stage] = default
    return AskClarificationRequest(
        status="clarification_required",
        kind="method_selection",
        session_id=plan.session_id,
        ask_plan_ref=ask_plan_ref,
        manifest_ref=plan.manifest_ref,
        question="Which methods should run for the selected stages?",
        choices={"methods_by_stage": methods_by_stage, "defaults": defaults},
        required_fields=["selected_methods"],
        response_schema={"kind": "method_selection", "selected_methods": defaults or {stage: "" for stage in methods_by_stage}},
        next_action=_next_action(ask_plan_ref),
    )


def _parameter_values_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    missing_fields, defaults, shared_defaults, stage_defaults, warnings = _missing_parameters_and_defaults(plan)
    all_fields: list[str] = []
    has_raw_long_reads = _has_raw_long_reads(plan)
    shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
    if has_raw_long_reads:
        all_fields.append("shared_parameters.platform")
    if (
        _assembly_needs_shared_read_type(plan)
        and "read_type" not in shared_defaults
        and (not has_raw_long_reads or "platform" in shared)
    ):
        all_fields.append("shared_parameters.read_type")
    if _genome_size_must_be_collected(plan):
        all_fields.append("shared_parameters.estimated_genome_size")
    for stage, method in _iter_selected_methods(plan):
        cap = _get_method_descriptor(method)
        if not cap:
            continue
        for param in cap["required_parameters"]:
            all_fields.append(f"{method}.{param}")
    if _function_annotation_needs_database_provenance(plan):
        all_fields.append(FUNCTION_DATABASE_PROVENANCE_FIELD)
    display_fields = all_fields if all_fields else missing_fields
    choices: dict[str, Any] = {"defaults": defaults, "shared_defaults": shared_defaults, "stage_defaults": stage_defaults}
    if _can_defer_genome_size_to_survey(plan):
        choices["deferred_shared_parameters"] = {
            "estimated_genome_size": {
                "value": shared_defaults.get("estimated_genome_size"),
                "source": "user_or_genome_survey",
                "status": "provided" if shared_defaults.get("estimated_genome_size") else "awaiting_genome_survey",
            }
        }
    if warnings:
        choices["warnings"] = warnings
    return AskClarificationRequest(
        status="clarification_required",
        kind="parameter_values",
        session_id=plan.session_id,
        ask_plan_ref=ask_plan_ref,
        manifest_ref=plan.manifest_ref,
        question="Confirm or edit parameters for executable methods.",
        choices=choices,
        required_fields=display_fields,
        response_schema={
            "kind": "parameter_values",
            "shared_parameters": shared_defaults,
            "method_parameters": defaults,
            "stage_parameters": stage_defaults,
        },
        next_action=_next_action(ask_plan_ref),
    )


def _confirm_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    summary = build_run_plan_summary(plan)
    return AskClarificationRequest(
        status="confirm_required",
        kind="confirm",
        session_id=plan.session_id,
        ask_plan_ref=ask_plan_ref,
        manifest_ref=plan.manifest_ref,
        question="Confirm the execution plan below and proceed?",
        summary=summary,
        required_fields=["confirmed"],
        response_schema={"kind": "confirm", "confirmed": True},
        next_action=_next_action(ask_plan_ref),
    )


def build_run_plan_summary(plan: AskPlanState) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "session_id": plan.session_id,
        "data_dir": plan.data_dir,
        "manifest_ref": plan.manifest_ref,
        "selected_stages": plan.selected_stages,
        "resolved_execution_order": plan.resolved_execution_order,
        "stage_roles": _classify_stage_roles(plan),
    }
    entry = plan.entry_request or {}
    if entry.get("intent_parse_source"):
        summary["intent_parse_source"] = entry["intent_parse_source"]
    if entry.get("source_paths"):
        summary["source_paths"] = entry["source_paths"]
    input_assets = _input_assets_from_manifest(plan.manifest_ref)
    if input_assets:
        summary["input_assets"] = input_assets
    if plan.selected_methods:
        summary["selected_methods"] = plan.selected_methods
    if plan.shared_parameters:
        summary["shared_parameters"] = plan.shared_parameters.model_dump(exclude_none=True)
    if plan.method_parameters:
        summary["method_parameters"] = plan.method_parameters
    if plan.stage_parameters:
        summary["stage_parameters"] = plan.stage_parameters
    runtime_parameter_contracts = _runtime_parameter_contracts(plan)
    if runtime_parameter_contracts:
        summary["runtime_parameter_contracts"] = runtime_parameter_contracts
    decision_points = _runtime_decision_points(plan.resolved_execution_order or [])
    if decision_points:
        summary["runtime_decision_points"] = decision_points
    # Load manifest once and pass it through to all parameter inference functions
    manifest = _load_manifest(plan.manifest_ref)
    missing_fields, parameter_defaults, shared_parameter_defaults, stage_parameter_defaults, param_warnings = (
        _missing_parameters_and_defaults(plan, manifest=manifest)
    )
    if parameter_defaults:
        summary["parameter_defaults"] = parameter_defaults
        summary["parameter_default_sources"] = _method_parameter_default_sources(plan, parameter_defaults, manifest=manifest)
    if shared_parameter_defaults:
        summary["shared_parameter_defaults"] = shared_parameter_defaults
        summary["shared_parameter_default_sources"] = _shared_parameter_default_sources(plan, shared_parameter_defaults, manifest=manifest)
    if stage_parameter_defaults:
        summary["stage_parameter_defaults"] = stage_parameter_defaults
    if missing_fields:
        summary["missing_parameter_fields"] = missing_fields
    deferred_shared = _deferred_shared_parameter_summary(plan, shared_parameter_defaults)
    if deferred_shared:
        summary["deferred_shared_parameters"] = deferred_shared
    stage_details = _stage_plan_details(
        plan,
        parameter_defaults=parameter_defaults,
        missing_fields=missing_fields,
        manifest=manifest,
    )
    if stage_details:
        summary["stage_plan_details"] = stage_details
    warnings = [*param_warnings, *_plan_validation_warnings(plan)]
    if warnings:
        summary["warnings"] = warnings
    try:
        summary["run_config_preview"] = build_run_config_payload(plan)
    except (ValueError, ValidationError) as exc:
        summary["run_config_preview"] = None
        summary["run_config_preview_error"] = str(exc)
    return summary


def _runtime_decision_points(stages: list[str]) -> list[dict[str, Any]]:
    """Describe future human interactions without fabricating their payloads.

    The review-gate registry is the single source of truth.  A plan only says
    that a proposal will be created later; the runtime interrupt and its
    artifacts remain authoritative for the actual values and choices.
    """

    points: list[dict[str, Any]] = []
    for gate in planned_review_gates(stages):
        point: dict[str, Any] = {
            "stage": gate.stage,
            "gate_id": gate.gate_id,
            "proposal_kind": gate.proposal_kind,
            "label": gate.proposal_label or gate.gate_id.replace("_", " ").title(),
            "status": "deferred",
            "trigger": (
                f"when {gate.stage} reaches the {gate.gate_id} gate and "
                "its prerequisite results/evidence are available"
            ),
            "decisions_allowed": list(gate.decisions_allowed),
            "parameter_recommendation_status": gate.parameter_recommendation_status,
        }
        related_parameter_gate = getattr(gate, "related_parameter_gate", None)
        if related_parameter_gate:
            point["related_parameter_gate"] = {
                "stage": related_parameter_gate[0],
                "gate_id": related_parameter_gate[1],
            }
        recommendation_notes = list(getattr(gate, "parameter_recommendation_notes", ()) or ())
        if recommendation_notes:
            point["parameter_recommendation_notes"] = recommendation_notes
        points.append(point)
    return points


def _runtime_parameter_contracts(plan: AskPlanState) -> list[dict[str, Any]]:
    """Expose catalog L2 fields as deferred, never as planning-time decisions."""

    from app.stages.assembly.parameters.catalog import load_parameter_catalog
    from app.stages.hic_scaffolding.parameters.catalog import (
        load_autohic_parameter_catalog,
    )

    contracts: list[dict[str, Any]] = []
    for stage, method in _iter_selected_methods(plan):
        if stage != "assembly":
            continue
        try:
            catalog = load_parameter_catalog(method)
        except (FileNotFoundError, ValueError):
            continue
        dynamic_names = [name for name, spec in catalog.parameters.items() if spec.level == 2]
        if not dynamic_names:
            continue
        supplied = dict((plan.method_parameters or {}).get(method) or {})
        constraints = {name: supplied[name] for name in dynamic_names if name in supplied}
        contract: dict[str, Any] = {
            "stage": stage,
            "method": method,
            "status": "deferred",
            "trigger_gate": {"stage": "assembly", "gate_id": "parameter_approval"},
            "parameter_names": dynamic_names,
        }
        if constraints:
            contract["user_constraints"] = constraints
        contracts.append(contract)

    selected_stages = set(plan.resolved_execution_order or plan.selected_stages or [])
    if "hic_scaffolding" in selected_stages:
        try:
            catalog = load_autohic_parameter_catalog()
        except (FileNotFoundError, ValueError):
            catalog = None
        if catalog is not None:
            dynamic_names = [
                name
                for name, spec in catalog.parameters.items()
                if spec.level == 2
            ]
            supplied = dict(
                (plan.method_parameters or {}).get(
                    "hic_scaffolding.candidate.autohic"
                )
                or {}
            )
            contract = {
                "stage": "hic_scaffolding",
                "method": "hic_scaffolding.candidate.autohic",
                "status": "deferred",
                "trigger_gate": {
                    "stage": "hic_scaffolding",
                    "gate_id": "parameter_approval",
                },
                "parameter_names": dynamic_names,
            }
            constraints = {
                name: supplied[name]
                for name in dynamic_names
                if name in supplied
            }
            if constraints:
                contract["user_constraints"] = constraints
            contracts.append(contract)
    return contracts


def _next_action(ask_plan_ref: str) -> dict[str, Any]:
    return {
        "kind": "provide_clarification_response",
        "command": f"gene-agent ask --ask-plan {ask_plan_ref} --clarification-response <answers.json>",
    }


def _method_parameter_default_sources(
    plan: AskPlanState,
    defaults: dict[str, dict[str, Any]],
    *,
    manifest: InputManifest | None = None,
) -> dict[str, dict[str, str]]:
    """Generate parameter default source labels. Pass manifest to avoid repeated disk loading."""
    sources: dict[str, dict[str, str]] = {}
    for method, values in defaults.items():
        method_sources: dict[str, str] = {}
        for key, value in values.items():
            source = "default"
            method_sources[key] = source
        if method_sources:
            sources[method] = method_sources
    return sources


def _shared_parameter_default_sources(
    plan: AskPlanState,
    defaults: dict[str, Any],
    *,
    manifest: InputManifest | None = None,
) -> dict[str, str]:
    """Generate shared parameter default source labels. Pass manifest to avoid repeated disk loading."""
    sources: dict[str, str] = {}
    entry = plan.entry_request or {}
    if manifest is None:
        manifest = _load_manifest(plan.manifest_ref)
    for key in defaults:
        source = "default"
        if key == "platform":
            source = "from request" if entry.get("platform") and entry.get("platform") != "unknown" else "from manifest"
        elif key == "read_type":
            source = "auto: derived from sequencing platform"
        elif key == "estimated_genome_size":
            source = "from request" if entry.get("genome_size_estimate") else "from manifest"
            if manifest and manifest.genome_size_estimate and not entry.get("genome_size_estimate"):
                source = "from manifest"
        sources[key] = source
    return sources


def _deferred_shared_parameter_summary(plan: AskPlanState, shared_defaults: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not _can_defer_genome_size_to_survey(plan):
        return {}
    value = shared_defaults.get("estimated_genome_size")
    if value is not None:
        return {}
    return {
        "estimated_genome_size": {
            "value": "auto",
            "source": "auto: await genome_survey result",
            "status": "awaiting_genome_survey",
        }
    }


def _stage_plan_details(
    plan: AskPlanState,
    *,
    parameter_defaults: dict[str, dict[str, Any]],
    missing_fields: list[str],
    manifest: InputManifest | None = None,
) -> list[dict[str, Any]]:
    if manifest is None:
        manifest = _load_manifest(plan.manifest_ref)
    asset_types = {asset.asset_type for asset in manifest.assets} if manifest else set()
    roles = _classify_stage_roles(plan)
    details: list[dict[str, Any]] = []
    for stage in plan.resolved_execution_order or []:
        selected = (plan.selected_methods or {}).get(stage)
        selected_methods = selected if isinstance(selected, list) else [selected] if selected else []
        stage_methods: list[dict[str, Any]] = []
        for method in selected_methods:
            method_missing = [
                field[len(method) + 1:]
                for field in missing_fields
                if field.startswith(f"{method}.")
            ]
            stage_methods.append(
                {
                    "method": method,
                    "parameters": dict(parameter_defaults.get(method) or {}),
                    "missing_parameters": method_missing,
                }
            )
        status = "configured" if stage_methods else _stage_without_method_status(stage)
        detail: dict[str, Any] = {
            "stage": stage,
            "role": roles.get(stage),
            "status": status,
            "methods": stage_methods,
            "inputs": _stage_input_status(stage, asset_types),
        }
        stage_missing = [
            field.split(".", 1)[1]
            for field in missing_fields
            if field.startswith(f"{stage}.") and not any(field.startswith(f"{method}.") for method in selected_methods)
        ]
        if stage_missing:
            detail["missing_parameters"] = stage_missing
        details.append(detail)
    return details


def _stage_without_method_status(stage: str) -> str:
    if stage in _STAGES_NEEDING_METHOD_SELECTION:
        return "method pending"
    if stage in load_planner_method_catalog().by_stage:
        return "pipeline/internal"
    return "no registered method yet"


def _stage_input_status(stage: str, asset_types: set[str]) -> list[str]:
    if stage == "hic_scaffolding" and not {"hic_reads_registered", "hic_alignment_bam", "hic_merged_nodups"} & asset_types:
        return ["missing Hi-C reads/alignment/merged_nodups in manifest"]
    if stage == "t2t" and not {"hic_reads_registered", "hic_alignment_bam", "hic_merged_nodups"} & asset_types:
        return ["T2T finishing usually needs Hi-C/contact-map or telomere evidence; none registered in manifest"]
    return []


def _all_selectable_stages() -> list[str]:
    return [s for s in STAGE_ORDER if s != "inventory"]


def _recommended_stages_from_manifest(manifest_ref: str) -> list[str]:
    manifest = _load_manifest(manifest_ref)
    if manifest is None:
        return []
    asset_types = {asset.asset_type for asset in manifest.assets}
    recommended: list[str] = []
    if "raw_long_reads" in asset_types:
        recommended.extend(["read_qc", "genome_survey", "assembly", "assembly_dedup", "assembly_review"])
    if "assembly_fasta_registered" in asset_types:
        recommended.append("assembly_review")
    if {"hic_reads_registered", "hic_alignment_bam", "hic_merged_nodups"} & asset_types:
        recommended.append("hic_scaffolding")
    has_assembly_path = "raw_long_reads" in asset_types or "assembly_fasta_registered" in asset_types
    if has_assembly_path:
        recommended.extend(["t2t", "repeat_annotation", "gene_annotation", "function_annotation"])
    if "repeat_library" in asset_types:
        recommended.append("repeat_annotation")
    if "raw_rnaseq_reads" in asset_types or "raw_isoseq_or_long_rna_reads" in asset_types:
        recommended.append("gene_annotation")
    if "gene_models" in asset_types or "predicted_proteins" in asset_types:
        recommended.extend(["gene_annotation", "function_annotation"])
    return list(dict.fromkeys(recommended))


def _suggested_stages(plan: AskPlanState, available: list[str]) -> list[str]:
    requested = [stage for stage in _intent_requested_stages(plan) if stage in available]
    if requested:
        return requested
    recommended = _recommended_stages_from_manifest(plan.manifest_ref)
    if recommended:
        return [s for s in recommended if s in available]
    return available[:2] if available else []


def _intent_requested_stages(plan: AskPlanState) -> list[str]:
    entry = plan.entry_request or {}
    requested = entry.get("requested_stages")
    if not isinstance(requested, list):
        return []
    available = set(_all_selectable_stages())
    return [str(stage) for stage in requested if str(stage) in available]


def _input_assets_from_manifest(manifest_ref: str) -> list[dict[str, Any]]:
    manifest = _load_manifest(manifest_ref)
    if manifest is None:
        return []
    return [
        {
            "asset_type": str(asset.asset_type),
            "platform": str(asset.platform),
            "format": str(asset.format),
            "paths": list(asset.paths),
        }
        for asset in manifest.assets
    ]


def _resolve_execution_order(selected: list[str]) -> list[str]:
    selected_set = set(selected)
    return [stage for stage in STAGE_ORDER if stage in selected_set]


def _check_conditional_requirements(
    method: str,
    cap: dict[str, Any],
    merged_params: dict[str, Any],
) -> list[str]:
    """Check conditional required parameters based on current parameter values."""
    missing = []
    conditional_reqs = cap.get("conditional_required_parameters", [])

    for rule in conditional_reqs:
        condition = rule.get("condition", {})
        # Check if all condition key-value pairs match
        if all(merged_params.get(k) == v for k, v in condition.items()):
            for param in rule.get("required", []):
                if param not in merged_params:
                    missing.append(f"{method}.{param}")

    return missing


def _needs_method_selection(plan: AskPlanState) -> bool:
    if plan.selected_methods is not None:
        # Check if all non-pipeline stages have methods selected
        for stage in plan.resolved_execution_order or []:
            if stage in _STAGES_NEEDING_METHOD_SELECTION and stage not in plan.selected_methods:
                return True
        return False
    return any(stage in _STAGES_NEEDING_METHOD_SELECTION for stage in plan.resolved_execution_order or [])


def _iter_selected_methods(plan: AskPlanState) -> list[tuple[str, str]]:
    """Yield (stage, method_name) pairs, flattening list values."""
    for stage, methods in (plan.selected_methods or {}).items():
        if isinstance(methods, list):
            for m in methods:
                yield stage, m
        else:
            yield stage, methods


def _validate_selected_methods(methods: dict[str, str | list[str]], plan: AskPlanState) -> None:
    known = _all_known_methods()
    for stage, value in methods.items():
        method_list = value if isinstance(value, list) else [value]
        stage_methods = {m["method_name"] for m in _executable_methods_for_stage(stage)}
        for method in method_list:
            if not method or not method.strip():
                raise ValueError(f"empty method name for stage {stage!r}")
            if method not in stage_methods:
                reason = "unknown method" if method not in known else "method is not valid"
                raise ValueError(
                    f"{reason} {method!r} for stage {stage!r}; "
                    f"known methods: {sorted(stage_methods)}"
                )
        if stage == "read_qc":
            filters = [method for method in method_list if method in READ_QC_FILTER_METHODS]
            if len(filters) != 1:
                raise ValueError("read_qc method selection requires exactly one length filter choice")
            if READ_QC_STATS_METHOD not in method_list:
                raise ValueError(f"read_qc method selection requires {READ_QC_STATS_METHOD}")
            if READ_QC_HANDOFF_METHOD not in method_list:
                raise ValueError(f"read_qc method selection requires {READ_QC_HANDOFF_METHOD}")
        if stage == "genome_survey":
            executable = set(method_list)
            if executable and executable != GENOME_SURVEY_EXECUTABLE_METHODS:
                raise ValueError(
                    "genome_survey executable selection requires both "
                    "genome_survey.kmer_count.jellyfish and genome_survey.model.genomescope2"
                )


def _normalize_selected_methods(methods: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
    normalized = dict(methods)
    if "read_qc" in normalized:
        selected = normalized["read_qc"]
        method_list = selected if isinstance(selected, list) else [selected]
        filter_method = next((method for method in method_list if method in READ_QC_FILTER_METHODS), READ_QC_FILTER_SKIP)
        normalized["read_qc"] = [READ_QC_STATS_METHOD, filter_method, READ_QC_HANDOFF_METHOD]
    return normalized


def _fill_missing_method_defaults(plan: AskPlanState, methods: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
    filled = dict(methods)
    for stage in plan.resolved_execution_order or []:
        if stage in filled or stage not in _STAGES_NEEDING_METHOD_SELECTION:
            continue
        default = _default_methods_for_stage(stage)
        if default:
            filled[stage] = default
    return filled


def _read_qc_length_filter_config(plan: AskPlanState) -> dict[str, Any]:
    selected = (plan.selected_methods or {}).get("read_qc") or list(READ_QC_DEFAULT_METHODS)
    method_list = selected if isinstance(selected, list) else [selected]
    filter_method = next((method for method in method_list if method in READ_QC_FILTER_METHODS), READ_QC_FILTER_SKIP)
    if filter_method == READ_QC_FILTER_SKIP:
        return {"method": "skip", "approved": False, "thresholds": {}}
    if filter_method == READ_QC_FILTER_SEQKIT:
        params = (plan.method_parameters or {}).get(READ_QC_FILTER_SEQKIT) or {}
        min_len = _coerce_positive_int(params.get("min_len"))
        if min_len is None:
            raise ValueError("read_qc.length_filter.seqkit_seq.min_len must be a positive integer")
        return {"method": filter_method, "approved": True, "thresholds": {"min_len": min_len}}
    return {"method": filter_method, "approved": False, "thresholds": {}}


def _build_genome_survey_config(plan: AskPlanState) -> dict[str, Any] | None:
    params = dict((plan.method_parameters or {}).get("genome_survey.kmer_count.jellyfish") or {})
    if not params:
        cap = _get_method_descriptor("genome_survey.kmer_count.jellyfish") or {}
        params = dict(cap.get("parameter_defaults") or {})
    survey_params: dict[str, Any] = {}
    for key in ("kmer_size", "hash_size", "threads"):
        if key in params:
            survey_params[key] = params[key]
    return {"survey_parameters": survey_params} if survey_params else None


def _build_function_annotation_config(plan: AskPlanState) -> dict[str, Any] | None:
    params = (plan.stage_parameters or {}).get("function_annotation") or {}
    provenance = params.get("database_provenance")
    if not provenance:
        methods = (plan.selected_methods or {}).get("function_annotation")
        method_list = methods if isinstance(methods, list) else [methods] if methods else []
        if plan.confirmed and FUNCTION_ANNOTATION_DIAMOND_METHOD in method_list:
            raise ValueError("function_annotation.database_provenance is required for DIAMOND execution")
        return None
    return {"database_provenance": provenance}


def _build_t2t_config(plan: AskPlanState) -> dict[str, Any]:
    params = dict((plan.method_parameters or {}).get("t2t.alignment.minimap2") or {})
    threshold_map = {
        "threshold_min_motif_copies": "min_motif_copies",
        "threshold_terminal_window_bp": "terminal_window_bp",
        "threshold_min_mapq": "min_mapq",
        "threshold_max_target_terminal_distance_bp": "max_target_terminal_distance_bp",
        "threshold_min_overlap_bp": "min_overlap_bp",
        "threshold_min_supporting_reads": "min_supporting_reads",
    }
    payload: dict[str, Any] = {}
    for key in ("mode", "telomere_motif", "chromosome_list_ref", "assembly_fasta_ref", "ont_reads_ref"):
        value = params.get(key)
        if value not in (None, ""):
            payload[key] = value
    thresholds = {
        out_key: params[in_key]
        for in_key, out_key in threshold_map.items()
        if params.get(in_key) not in (None, "")
    }
    if thresholds:
        payload["thresholds"] = thresholds
    return payload


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _needs_parameter_values(plan: AskPlanState) -> bool:
    """True when there are executable methods with parameters to show/confirm."""
    shared_defaults, _ = _infer_shared_parameter_defaults(plan)
    shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
    method_overrides = _method_specific_shared_overrides(plan)
    merged_shared = _derive_shared_read_type_from_platform({**shared_defaults, **shared, **method_overrides})
    if _has_raw_long_reads(plan) and "platform" not in merged_shared:
        return True
    if _assembly_needs_shared_read_type(plan) and "read_type" not in merged_shared:
        return True
    if _genome_size_must_be_collected(plan) and "estimated_genome_size" not in merged_shared:
        return True
    if (plan.resolved_execution_order or []) and any(
        method in _ASSEMBLY_METHODS_NEED_SHARED_READ_TYPE
        for _, method in _iter_selected_methods(plan)
    ) and plan.shared_parameters is None and not method_overrides:
        return True
    for stage, method in _iter_selected_methods(plan):
        cap = _get_method_descriptor(method)
        if not cap:
            continue
        if cap["required_parameters"]:
            provided = (plan.method_parameters or {}).get(method, {})
            inferred, _ = _infer_parameter_defaults(plan, method, cap)
            merged = {**inferred, **provided}
            for param in cap["required_parameters"]:
                if param not in merged:
                    return True
            if not provided:
                return True
    if _function_annotation_needs_database_provenance(plan):
        return True
    return False


def _auto_fill_inferred_defaults(plan: AskPlanState) -> AskPlanState:
    """Merge inferred defaults into method_parameters for any missing keys."""
    _, defaults, shared_defaults, _, _ = _missing_parameters_and_defaults(plan)
    update: dict[str, Any] = {}
    if defaults:
        update["method_parameters"] = defaults
    if shared_defaults:
        current_shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
        current_shared = _derive_shared_read_type_from_platform({**shared_defaults, **current_shared})
        update["shared_parameters"] = SharedParameters.model_validate(current_shared)
    if not update:
        return plan
    return plan.model_copy(update=update)


def _missing_parameters_and_defaults(
    plan: AskPlanState,
    *,
    manifest: InputManifest | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    """Compute missing parameters and defaults. Pass manifest to avoid repeated disk loading."""
    missing_fields: list[str] = []
    defaults: dict[str, dict[str, Any]] = {}
    stage_defaults: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    if manifest is None:
        manifest = _load_manifest(plan.manifest_ref)
    shared_defaults, shared_warnings = _infer_shared_parameter_defaults(plan, manifest=manifest)
    warnings.extend(shared_warnings)
    shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
    shared_defaults = _derive_shared_read_type_from_platform(
        {**shared_defaults, **shared, **_method_specific_shared_overrides(plan)}
    )
    if _assembly_needs_shared_read_type(plan) and "read_type" not in shared_defaults:
        missing_fields.append("shared_parameters.read_type")
    if _genome_size_must_be_collected(plan) and "estimated_genome_size" not in shared_defaults:
        missing_fields.append("shared_parameters.estimated_genome_size")
    for stage, method in _iter_selected_methods(plan):
        cap = _get_method_descriptor(method)
        if not cap:
            continue
        provided = (plan.method_parameters or {}).get(method, {})
        inferred, method_warnings = _infer_parameter_defaults(plan, method, cap, manifest=manifest)
        warnings.extend(method_warnings)
        merged = {**inferred, **provided}
        defaults[method] = merged
        for param in cap["required_parameters"]:
            if param not in merged:
                missing_fields.append(f"{method}.{param}")
        # Check conditional requirements declared in method descriptor
        missing_fields.extend(_check_conditional_requirements(method, cap, merged))
    function_params = dict((plan.stage_parameters or {}).get("function_annotation") or {})
    if function_params:
        stage_defaults["function_annotation"] = function_params
    if _function_annotation_needs_database_provenance(plan):
        missing_fields.append(FUNCTION_DATABASE_PROVENANCE_FIELD)
    return missing_fields, defaults, shared_defaults, stage_defaults, warnings


def _function_annotation_needs_database_provenance(plan: AskPlanState) -> bool:
    methods = (plan.selected_methods or {}).get("function_annotation")
    method_list = methods if isinstance(methods, list) else [methods] if methods else []
    if FUNCTION_ANNOTATION_DIAMOND_METHOD not in method_list:
        return False
    provenance = ((plan.stage_parameters or {}).get("function_annotation") or {}).get("database_provenance")
    return not provenance


def _infer_parameter_defaults(
    plan: AskPlanState,
    method: str,
    cap: dict[str, Any],
    *,
    manifest: InputManifest | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Infer parameter defaults for a method. Pass manifest to avoid repeated disk loading."""
    inferred = dict(cap.get("parameter_defaults") or {})
    warnings: list[str] = []
    if method == "assembly_review.eval.busco":
        provided = (plan.method_parameters or {}).get(method, {})
        lineage_path = provided.get("lineage_path") or inferred.get("lineage_path")
        if lineage_path:
            if not Path(lineage_path).exists():
                warnings.append(
                    f"assembly_review.eval.busco: lineage_path '{lineage_path}' does not exist; "
                    "BUSCO will be skipped at runtime"
                )
            lp = Path(lineage_path)
            name = lp.name
            if "lineage_name" not in inferred:
                inferred["lineage_name"] = name
            version = _extract_busco_version(name)
            if version and "lineage_version" not in inferred:
                inferred["lineage_version"] = version
            if "lineage_source" not in inferred:
                inferred["lineage_source"] = "local"
    return inferred, warnings


def _extract_busco_version(lineage_name: str) -> str:
    import re
    m = re.search(r"(odb\d+)", lineage_name)
    return m.group(1) if m else ""


def _infer_shared_parameter_defaults(
    plan: AskPlanState,
    *,
    manifest: InputManifest | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Infer shared parameter defaults. Pass manifest to avoid repeated disk loading."""
    inferred: dict[str, Any] = {}
    warnings: list[str] = []
    if manifest is None:
        manifest = _load_manifest(plan.manifest_ref)
    if manifest:
        platforms = _raw_long_read_platforms(manifest, include_unknown=False)
        all_platforms = _raw_long_read_platforms(manifest, include_unknown=True)
        if len(all_platforms) == 1:
            inferred.setdefault("platform", next(iter(all_platforms)))
        elif len(platforms) == 1:
            inferred.setdefault("platform", next(iter(platforms)))
        for platform, read_type in PLATFORM_TO_SHARED_READ_TYPE.items():
            if platform in platforms:
                inferred.setdefault("read_type", read_type)
                break
        if manifest.genome_size_estimate:
            inferred.setdefault("estimated_genome_size", manifest.genome_size_estimate)
    entry = plan.entry_request or {}
    if entry.get("genome_size_estimate"):
        manifest_size = (manifest.genome_size_estimate if manifest else None)
        request_size = entry["genome_size_estimate"]
        if manifest_size is not None and manifest_size != request_size:
            warnings.append(
                f"genome_size conflict: manifest says {manifest_size}, "
                f"request says {request_size}; using manifest value"
            )
        else:
            inferred.setdefault("estimated_genome_size", request_size)
    return _derive_shared_read_type_from_platform(inferred), warnings


def _derive_shared_read_type_from_platform(shared: dict[str, Any]) -> dict[str, Any]:
    if "read_type" in shared:
        return shared
    read_type = PLATFORM_TO_SHARED_READ_TYPE.get(str(shared.get("platform") or ""))
    if not read_type:
        return shared
    return {**shared, "read_type": read_type}


def _has_raw_long_reads(plan: AskPlanState) -> bool:
    manifest = _load_manifest(plan.manifest_ref)
    return bool(manifest and any(asset.asset_type == "raw_long_reads" for asset in manifest.assets))


def _raw_long_read_platforms(manifest: Any, *, include_unknown: bool) -> set[str]:
    return {
        str(asset.platform)
        for asset in manifest.assets
        if asset.asset_type == "raw_long_reads" and (include_unknown or asset.platform != "unknown")
    }


def _assembly_needs_shared_read_type(plan: AskPlanState) -> bool:
    return any(method in _ASSEMBLY_METHODS_NEED_SHARED_READ_TYPE for _, method in _iter_selected_methods(plan))


def _assembly_needs_shared_genome_size(plan: AskPlanState) -> bool:
    return any(method in _ASSEMBLY_METHODS_NEED_SHARED_GENOME_SIZE for _, method in _iter_selected_methods(plan))


def _can_defer_genome_size_to_survey(plan: AskPlanState) -> bool:
    """Defer when executable Survey precedes assembly with short or long genomic reads."""
    order = plan.resolved_execution_order or []
    if "genome_survey" not in order or "assembly" not in order:
        return False
    if order.index("genome_survey") > order.index("assembly"):
        return False
    selected = (plan.selected_methods or {}).get("genome_survey")
    method_list = selected if isinstance(selected, list) else [selected] if selected else []
    executable = set(method_list)
    if executable != GENOME_SURVEY_EXECUTABLE_METHODS:
        return False
    manifest = _load_manifest(plan.manifest_ref)
    return bool(
        manifest
        and any(
            asset.asset_type
            in {"raw_short_dna_reads", "clean_short_dna_reads", "raw_long_reads", "clean_long_reads"}
            for asset in manifest.assets
        )
    )


def _genome_size_must_be_collected(plan: AskPlanState) -> bool:
    return _assembly_needs_shared_genome_size(plan) and not _can_defer_genome_size_to_survey(plan)


def _method_specific_shared_overrides(plan: AskPlanState) -> dict[str, Any]:
    flye = (plan.method_parameters or {}).get("assembly.candidate.flye") or {}
    shared: dict[str, Any] = {}
    if flye.get("read_type"):
        mapped = FLYE_TO_SHARED_READ_TYPE.get(str(flye["read_type"]))
        if mapped:
            shared["read_type"] = mapped
    if flye.get("estimated_genome_size"):
        shared["estimated_genome_size"] = flye["estimated_genome_size"]
    return shared


def _stage_has_method(plan: AskPlanState, stage: str) -> bool:
    if stage == "read_qc":
        return stage in (plan.resolved_execution_order or []) or stage in (plan.selected_stages or [])
    value = (plan.selected_methods or {}).get(stage)
    if not value:
        return _STAGE_SELECTION_MODE.get(stage) == "pipeline"
    methods = value if isinstance(value, list) else [value]
    return any(_get_method_descriptor(m) is not None for m in methods)


def _classify_stage_roles(plan: AskPlanState) -> dict[str, str]:
    """Label each stage in execution order as executable or dependency."""
    selected = set(plan.selected_stages or [])
    roles: dict[str, str] = {}
    for stage in plan.resolved_execution_order or []:
        if _stage_has_method(plan, stage):
            if stage in selected:
                roles[stage] = "executable"
            else:
                roles[stage] = "dependency"
        elif stage not in selected:
            roles[stage] = "dependency"
        else:
            roles[stage] = "executable"
    return roles


def _plan_validation_warnings(plan: AskPlanState) -> list[str]:
    warnings: list[str] = []
    manifest = _load_manifest(plan.manifest_ref)
    asset_types = {asset.asset_type for asset in manifest.assets} if manifest else set()
    order = plan.resolved_execution_order or []
    if "hic_scaffolding" in order:
        if not {"hic_reads_registered", "hic_alignment_bam", "hic_merged_nodups"} & asset_types:
            warnings.append("hic_scaffolding: no Hi-C reads in manifest; selected stage will block on missing inputs")
    assembly_available = "assembly" in order or "assembly_fasta_registered" in asset_types
    if "assembly_review" in order and not assembly_available:
        warnings.append(
            "assembly_review: no assembly stage upstream and no "
            "assembly_fasta_registered in manifest; stage will fail without an assembly"
        )
    if "gene_annotation" in order and not assembly_available:
        has_provided = "gene_models" in asset_types and "predicted_proteins" in asset_types
        if not has_provided:
            warnings.append(
                "gene_annotation: no assembly upstream and no provided "
                "gene_models/predicted_proteins in manifest; stage will fail without a genome"
            )
    if "function_annotation" in order and "gene_annotation" not in order:
        if "predicted_proteins" not in asset_types and "gene_models" not in asset_types:
            warnings.append(
                "function_annotation: no gene_annotation upstream and no "
                "predicted_proteins/gene_models in manifest; stage will fail without protein input"
            )
    return warnings


def _load_manifest(manifest_ref: str) -> InputManifest | None:
    path = Path(manifest_ref)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
        return InputManifest.model_validate(raw)
    except Exception:
        return None
