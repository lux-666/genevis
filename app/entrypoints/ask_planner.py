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
from app.schemas.run_config import HiCScaffoldingPlanCandidate, Juicer3DDNARunParameters, RunConfig
from app.schemas.shared_parameters import FLYE_TO_SHARED_READ_TYPE, PLATFORM_TO_SHARED_READ_TYPE, SharedParameters
from app.stages import STAGE_ORDER

FUNCTION_DATABASE_PROVENANCE_FIELD = "function_annotation.database_provenance"
FUNCTION_ANNOTATION_DIAMOND_METHOD = "function_annotation.candidate.diamond"

_ASSEMBLY_METHODS_NEED_SHARED_GENOME_SIZE = {
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
}
_ASSEMBLY_METHODS_NEED_SHARED_READ_TYPE = {
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
    "assembly.candidate.hifiasm",
}
_GENOME_SURVEY_EXECUTABLE_METHODS = {
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.model.genomescope2",
}

# ---------------------------------------------------------------------------
# Unified method registry — all stages and their methods
# ---------------------------------------------------------------------------

_STAGE_SELECTION_MODE: dict[str, str] = {
    "read_qc": "read_qc_filter_choice",
    "genome_survey": "choose_many",
    "assembly": "choose_many",
    "assembly_review": "choose_many",
    "hic_scaffolding": "choose_many",
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
    "read_qc.length_filter.chopper",
    "read_qc.length_filter.filtlong",
    "read_qc.length_filter.fastplong",
]
READ_QC_DEFAULT_METHODS = [READ_QC_STATS_METHOD, READ_QC_FILTER_SKIP, READ_QC_HANDOFF_METHOD]

_STAGE_METHODS: dict[str, list[dict[str, Any]]] = {
    "read_qc": [
        {
            "method_name": READ_QC_STATS_METHOD,
            "execution_support": "executable",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "always",
        },
        {
            "method_name": READ_QC_FILTER_SKIP,
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "length_filter",
            "display_name": "skip filtering, use raw reads",
        },
        {
            "method_name": READ_QC_FILTER_SEQKIT,
            "execution_support": "executable",
            "required_parameters": ["min_len"],
            "parameter_defaults": {},
            "selection_role": "length_filter",
        },
        {
            "method_name": "read_qc.length_filter.chopper",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "length_filter",
        },
        {
            "method_name": "read_qc.length_filter.filtlong",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "length_filter",
        },
        {
            "method_name": "read_qc.length_filter.fastplong",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "length_filter",
        },
        {
            "method_name": READ_QC_HANDOFF_METHOD,
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
            "selection_role": "always",
        },
    ],
    "genome_survey": [
        {
            "method_name": "genome_survey.kmer_count.jellyfish",
            "execution_support": "executable",
            "required_parameters": ["kmer_size", "hash_size", "threads"],
            "parameter_defaults": {"kmer_size": 21, "hash_size": "1G", "threads": 4},
        },
        {
            "method_name": "genome_survey.model.genomescope2",
            "execution_support": "executable",
            "required_parameters": ["kmer_size"],
            "parameter_defaults": {"kmer_size": 21},
        },
        {
            "method_name": "genome_survey.kmer_count.meryl",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "genome_survey.model.smudgeplot",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "assembly": [
        {
            "method_name": "assembly.candidate.flye",
            "execution_support": "executable",
            "required_parameters": ["threads"],
            "parameter_defaults": {"threads": 4},
        },
        {
            "method_name": "assembly.candidate.nextdenovo",
            "execution_support": "executable",
            "required_parameters": ["threads"],
            "parameter_defaults": {"threads": 4},
        },
        {
            "method_name": "assembly.candidate.canu",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "assembly.candidate.hifiasm",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "assembly_review": [
        {
            "method_name": "assembly_review.eval.quast",
            "execution_support": "executable",
            "required_parameters": ["threads"],
            "parameter_defaults": {"threads": 4},
        },
        {
            "method_name": "assembly_review.eval.busco",
            "execution_support": "executable",
            "required_parameters": ["lineage_path", "lineage_name", "lineage_version", "lineage_source", "threads"],
            "parameter_defaults": {"threads": 8},
        },
        {
            "method_name": "assembly_review.eval.merqury",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "assembly_review.eval.mapping_coverage",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "assembly_review.check.contamination",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "assembly_review.check.purge_dups",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "hic_scaffolding": [
        {
            "method_name": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
            "execution_support": "executable",
            "required_parameters": ["input_mode", "chromosome_count"],
            "parameter_defaults": {"input_mode": "raw_reads"},
        },
        {
            "method_name": "hic_scaffolding.candidate.yahs",
            "execution_support": "executable",
            "required_parameters": ["hic_alignment_ref"],
            "parameter_defaults": {},
        },
    ],
    "repeat_annotation": [
        {
            "method_name": "repeat_annotation.masking.repeatmasker",
            "execution_support": "executable",
            "required_parameters": ["threads"],
            "parameter_defaults": {"threads": 8},
        },
        {
            "method_name": "repeat_annotation.library.edta",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "repeat_annotation.library.repeatmodeler",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "repeat_annotation.helper.trf",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "gene_annotation": [
        {
            "method_name": "gene_annotation.candidate.augustus",
            "execution_support": "executable",
            "category": "ab_initio",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.annevo",
            "execution_support": "planned_only",
            "category": "ab_initio_deep_learning",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.miniprot",
            "execution_support": "executable",
            "category": "homology",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.genewise",
            "execution_support": "planned_only",
            "category": "homology_high_accuracy",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.gemoma",
            "execution_support": "planned_only",
            "category": "homology_reference_annotation",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.braker3",
            "execution_support": "planned_only",
            "category": "evidence_guided_ab_initio",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.candidate.maker",
            "execution_support": "planned_only",
            "category": "integration_pipeline",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.helper.hisat2",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.helper.stringtie",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "gene_annotation.helper.isoquant",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "function_annotation": [
        {
            "method_name": "function_annotation.candidate.interproscan",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "function_annotation.candidate.eggnog_mapper",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "function_annotation.candidate.diamond",
            "execution_support": "executable",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "function_annotation.candidate.kofamscan",
            "execution_support": "planned_only",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
}

_AR_RUN_CONFIG_METHODS: dict[str, tuple[str, list[str], dict[str, Any]]] = {
    "assembly_review.eval.quast": ("quast", ["threads"], {"threads": 4}),
    "assembly_review.eval.busco": (
        "busco",
        ["lineage_path", "lineage_name", "lineage_version", "lineage_source", "threads"],
        {"threads": 8},
    ),
}


def _build_assembly_review_config(plan: AskPlanState) -> dict[str, Any]:
    ar_config: dict[str, Any] = {}
    for method_name, (config_key, required, defaults) in _AR_RUN_CONFIG_METHODS.items():
        params = (plan.method_parameters or {}).get(method_name)
        if not params:
            continue
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
    for methods in _STAGE_METHODS.values():
        for m in methods:
            if m["method_name"] == method_name:
                return m
    return None


def _all_known_methods() -> set[str]:
    return {m["method_name"] for methods in _STAGE_METHODS.values() for m in methods}


def _executable_methods_for_stage(stage: str) -> list[dict[str, Any]]:
    return [m for m in _STAGE_METHODS.get(stage, []) if m["execution_support"] == "executable"]


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
        return ["hic_scaffolding.candidate.juicer_3ddna_juicebox", "hic_scaffolding.candidate.yahs"]
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
    """Find a persisted clarification plan in either supported layout."""
    current = ask_plan_path(data_dir, session_id)
    if current.exists():
        return current
    legacy = Path(data_dir) / "ask_plans" / session_id / "ask_plan.json"
    return legacy if legacy.exists() else current


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

    Stages with a clear default are filled; genuinely multi-candidate stages
    (e.g. genome_survey, assembly_review) are left for method selection.
    """
    filled = _fill_missing_method_defaults(plan, dict(plan.selected_methods or {}))
    if "assembly" in (plan.resolved_execution_order or []) and "assembly" not in filled:
        manifest = _load_manifest(plan.manifest_ref)
        asset_types = {asset.asset_type for asset in manifest.assets} if manifest else set()
        if asset_types == {"raw_long_reads"}:
            filled["assembly"] = "assembly.candidate.flye"
    if filled == (plan.selected_methods or {}):
        return plan
    return plan.model_copy(update={"selected_methods": filled or None})


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
        if _stage_has_executable_method(plan, stage):
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
    executable = [
        method
        for method in methods
        if (_get_method_descriptor(method) or {}).get("execution_support") == "executable"
    ]
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
            genome_key = "estimated_genome_size" if config_method == "flye" else "genome_size"
            params.setdefault(genome_key, shared.estimated_genome_size)

    parameter_key = f"{config_method}_parameters"
    return {"assembly_method": config_method, parameter_key: params}


def _build_hic_scaffolding_config(plan: AskPlanState) -> dict[str, Any] | None:
    """Build a strict execution config from only ready Hi-C plan candidates."""
    candidates = _hic_scaffolding_plan_candidates(plan)
    ready = [candidate for candidate in candidates if candidate.status == "ready_to_execute"]
    if not ready:
        return None

    payload: dict[str, Any] = {
        "methods": [candidate.method for candidate in ready],
        "method": ready[0].method,
    }
    juicer = next((candidate for candidate in ready if candidate.method == "juicer_3ddna_juicebox"), None)
    if juicer:
        payload.update(
            {
                "input_mode": juicer.input_mode,
                "chromosome_count": juicer.chromosome_count,
                "merged_nodups_ref": juicer.merged_nodups_ref,
                "juicer_3ddna_parameters": juicer.juicer_3ddna_parameters.model_dump(exclude_none=True)
                if juicer.juicer_3ddna_parameters
                else None,
            }
        )
    yahs = next((candidate for candidate in ready if candidate.method == "yahs"), None)
    if yahs:
        payload["hic_alignment_ref"] = yahs.hic_alignment_ref
    return {key: value for key, value in payload.items() if value is not None}


def _hic_scaffolding_plan_candidates(plan: AskPlanState) -> list[HiCScaffoldingPlanCandidate]:
    """Represent selected methods as permissive plan candidates before execution."""
    selected = (plan.selected_methods or {}).get("hic_scaffolding")
    methods = selected if isinstance(selected, list) else [selected] if selected else []
    executable = [
        method
        for method in methods
        if (_get_method_descriptor(method) or {}).get("execution_support") == "executable"
    ]
    candidates: list[HiCScaffoldingPlanCandidate] = []
    for method in executable:
        params = dict((plan.method_parameters or {}).get(method) or {})
        if method.endswith("juicer_3ddna_juicebox"):
            tool_keys = {
                "restriction_enzyme",
                "chrom_sizes_ref",
                "threads",
                "juicer_command",
                "juicer_extra_args",
                "three_dna_command",
                "three_dna_extra_args",
            }
            tool_params = {key: value for key, value in params.items() if key in tool_keys}
            candidates.append(
                HiCScaffoldingPlanCandidate(
                    method="juicer_3ddna_juicebox",
                    input_mode=params.get("input_mode"),
                    chromosome_count=params.get("chromosome_count"),
                    merged_nodups_ref=params.get("merged_nodups_ref"),
                    juicer_3ddna_parameters=Juicer3DDNARunParameters.model_validate(tool_params),
                )
            )
        else:
            candidates.append(
                HiCScaffoldingPlanCandidate(
                    method="yahs",
                    hic_alignment_ref=params.get("hic_alignment_ref"),
                )
            )
    return candidates


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
            executable = [m["method_name"] for m in methods if m.get("execution_support") == "executable"]
            selected[stage] = executable[0] if executable else (methods[0]["method_name"] if methods else "")
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
        stage_methods = _STAGE_METHODS.get(stage, [])
        if stage_methods:
            methods_by_stage[stage] = [
                {
                    "method_name": m["method_name"],
                    "execution_support": m["execution_support"],
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
        if not cap or cap["execution_support"] != "executable":
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
    hic_candidates = _hic_scaffolding_plan_candidates(plan)
    if hic_candidates:
        summary["hic_scaffolding_plan_candidates"] = [candidate.model_dump(mode="json") for candidate in hic_candidates]
    method_status: dict[str, str] = {}
    for stage, method in _iter_selected_methods(plan):
        cap = _get_method_descriptor(method)
        method_status[method] = cap["execution_support"] if cap else "unknown"
    if method_status:
        summary["method_execution_status"] = method_status
    _, _, _, _, param_warnings = _missing_parameters_and_defaults(plan)
    warnings = [*param_warnings, *_plan_validation_warnings(plan)]
    if warnings:
        summary["warnings"] = warnings
    try:
        summary["run_config_preview"] = build_run_config_payload(plan)
    except (ValueError, ValidationError) as exc:
        summary["run_config_preview"] = None
        summary["run_config_preview_error"] = str(exc)
    return summary


def _next_action(ask_plan_ref: str) -> dict[str, Any]:
    return {
        "kind": "provide_clarification_response",
        "command": f"gene-agent ask --ask-plan {ask_plan_ref} --clarification-response <answers.json>",
    }


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
        recommended.extend(["repeat_annotation", "gene_annotation", "function_annotation"])
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
        stage_methods = {m["method_name"] for m in _STAGE_METHODS.get(stage, [])}
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
            executable = {
                method
                for method in method_list
                if (_get_method_descriptor(method) or {}).get("execution_support") == "executable"
            }
            if executable and executable != _GENOME_SURVEY_EXECUTABLE_METHODS:
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
        return None
    return {"database_provenance": provenance}


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
        if not cap or cap["execution_support"] != "executable":
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
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    missing_fields: list[str] = []
    defaults: dict[str, dict[str, Any]] = {}
    stage_defaults: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    shared_defaults, shared_warnings = _infer_shared_parameter_defaults(plan)
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
        if not cap or cap["execution_support"] != "executable":
            continue
        provided = (plan.method_parameters or {}).get(method, {})
        inferred, method_warnings = _infer_parameter_defaults(plan, method, cap)
        warnings.extend(method_warnings)
        merged = {**inferred, **provided}
        defaults[method] = merged
        for param in cap["required_parameters"]:
            if param not in merged:
                missing_fields.append(f"{method}.{param}")
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


def _infer_parameter_defaults(plan: AskPlanState, method: str, cap: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
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


def _infer_shared_parameter_defaults(plan: AskPlanState) -> tuple[dict[str, Any], list[str]]:
    inferred: dict[str, Any] = {}
    warnings: list[str] = []
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
    """Defer only when executable Survey will run before assembly with DNA reads."""
    order = plan.resolved_execution_order or []
    if "genome_survey" not in order or "assembly" not in order:
        return False
    if order.index("genome_survey") > order.index("assembly"):
        return False
    selected = (plan.selected_methods or {}).get("genome_survey")
    method_list = selected if isinstance(selected, list) else [selected] if selected else []
    executable = {
        method
        for method in method_list
        if (_get_method_descriptor(method) or {}).get("execution_support") == "executable"
    }
    if executable != _GENOME_SURVEY_EXECUTABLE_METHODS:
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


def _stage_has_executable_method(plan: AskPlanState, stage: str) -> bool:
    if stage == "read_qc":
        return stage in (plan.resolved_execution_order or []) or stage in (plan.selected_stages or [])
    value = (plan.selected_methods or {}).get(stage)
    if not value:
        return _STAGE_SELECTION_MODE.get(stage) == "pipeline"
    methods = value if isinstance(value, list) else [value]
    return any(
        (cap := _get_method_descriptor(m)) is not None and cap["execution_support"] == "executable"
        for m in methods
    )


def _classify_stage_roles(plan: AskPlanState) -> dict[str, str]:
    """Label each stage in execution order as executable, dependency, or planned_only."""
    selected = set(plan.selected_stages or [])
    roles: dict[str, str] = {}
    for stage in plan.resolved_execution_order or []:
        if _stage_has_executable_method(plan, stage):
            if stage in selected:
                roles[stage] = "executable"
            else:
                roles[stage] = "dependency"
        else:
            value = (plan.selected_methods or {}).get(stage)
            methods = value if isinstance(value, list) else [value] if value else []
            has_planned = any(
                (cap := _get_method_descriptor(m)) is not None and cap["execution_support"] == "planned_only"
                for m in methods
            )
            if has_planned:
                roles[stage] = "planned_only"
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
            warnings.append("hic_scaffolding: no Hi-C reads in manifest; stage will skip at runtime")
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
    for stage in order:
        methods = (plan.selected_methods or {}).get(stage)
        if not methods:
            continue
        method_list = methods if isinstance(methods, list) else [methods]
        all_planned = all(
            (cap := _get_method_descriptor(m)) is not None and cap["execution_support"] == "planned_only"
            for m in method_list
        )
        if all_planned:
            warnings.append(f"{stage}: all selected methods are planned_only (execution deferred, not local)")
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
