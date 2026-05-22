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
from app.schemas.run_config import RunConfig
from app.stages import STAGE_ORDER

_FLYE_READ_TYPE_MAP = {"cycloneseq": "nano-raw", "ont": "nano-raw", "clr": "pacbio-raw"}

# ---------------------------------------------------------------------------
# Unified method registry — all stages and their methods
# ---------------------------------------------------------------------------

_STAGE_SELECTION_MODE: dict[str, str] = {
    "read_qc": "pipeline",
    "genome_survey": "choose_many",
    "assembly": "choose_many",
    "assembly_review": "choose_many",
}

_STAGE_METHODS: dict[str, list[dict[str, Any]]] = {
    "read_qc": [
        {
            "method_name": "read_qc.long_read_stats.seqkit_stats",
            "execution_support": "executable",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "read_qc.length_filter.seqkit_seq",
            "execution_support": "executable",
            "required_parameters": [],
            "parameter_defaults": {},
        },
        {
            "method_name": "read_qc.make_fofn.local",
            "execution_support": "executable",
            "required_parameters": [],
            "parameter_defaults": {},
        },
    ],
    "genome_survey": [
        {
            "method_name": "genome_survey.kmer_count.jellyfish",
            "execution_support": "executable",
            "required_parameters": ["kmer_size", "hash_size", "read_length", "threads"],
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
            "required_parameters": ["read_type", "estimated_genome_size", "threads"],
            "parameter_defaults": {"threads": 4},
        },
        {
            "method_name": "assembly.candidate.nextdenovo",
            "execution_support": "planned_only",
            "required_parameters": ["estimated_genome_size"],
            "parameter_defaults": {},
        },
        {
            "method_name": "assembly.candidate.canu",
            "execution_support": "planned_only",
            "required_parameters": ["estimated_genome_size"],
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
            "execution_support": "planned_only",
            "required_parameters": ["lineage", "threads"],
            "parameter_defaults": {"threads": 8},
        },
        {
            "method_name": "assembly_review.check.merqury",
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
}

_STAGES_NEEDING_METHOD_SELECTION = {
    stage for stage, mode in _STAGE_SELECTION_MODE.items() if mode != "pipeline"
}

_ASSEMBLY_UPSTREAM_DEPS = {
    "assembly": ["read_qc"],
    "assembly_review": ["read_qc", "assembly"],
    "hic_scaffolding": ["read_qc", "assembly", "assembly_review"],
    "repeat_annotation": ["read_qc", "assembly", "assembly_review"],
    "gene_annotation": ["read_qc", "assembly", "assembly_review", "repeat_annotation"],
    "function_annotation": ["read_qc", "assembly", "assembly_review", "repeat_annotation", "gene_annotation"],
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


def ask_plan_path(data_dir: str | Path, session_id: str) -> Path:
    return Path(data_dir) / "ask_plans" / session_id / "ask_plan.json"


def save_plan_to_default_path(plan: AskPlanState) -> Path:
    return save_ask_plan(plan, ask_plan_path(plan.data_dir, plan.session_id))


def auto_resolve_plan(plan: AskPlanState) -> AskPlanState:
    """Auto-select assembly/flye when the manifest contains only raw long reads."""
    if plan.selected_stages is not None:
        return plan
    manifest = _load_manifest(plan.manifest_ref)
    if manifest is None:
        return plan
    asset_types = {asset.asset_type for asset in manifest.assets}
    if asset_types != {"raw_long_reads"}:
        return plan
    stages = ["assembly"]
    order = _resolve_execution_order(stages, plan)
    plan = plan.model_copy(update={
        "selected_stages": stages,
        "resolved_execution_order": order,
        "selected_methods": {"assembly": "assembly.candidate.flye"},
    })
    return plan


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
        order = _resolve_execution_order(stages, plan)
        pipeline_methods = _auto_fill_pipeline_methods(order)
        return plan.model_copy(update={
            "selected_stages": stages,
            "resolved_execution_order": order,
            "selected_methods": pipeline_methods or None,
            "method_parameters": None,
            "confirmed": False,
        })
    if response.kind == "method_selection":
        methods = dict(response.selected_methods or {})
        existing = dict(plan.selected_methods or {})
        existing.update(methods)
        _validate_selected_methods(existing, plan)
        return plan.model_copy(update={
            "selected_methods": existing,
            "method_parameters": None,
            "confirmed": False,
        })
    if response.kind == "parameter_values":
        existing = dict(plan.method_parameters or {})
        existing.update(response.method_parameters or {})
        return plan.model_copy(update={"method_parameters": existing, "confirmed": False})
    if response.kind == "confirm":
        return plan.model_copy(update={"confirmed": bool(response.confirmed)})
    return plan


def build_run_config_payload(plan: AskPlanState) -> dict[str, Any]:
    local_stages = []
    for stage in plan.resolved_execution_order or []:
        if _stage_has_executable_method(plan, stage):
            local_stages.append(stage)

    payload: dict[str, Any] = {"local_exec": local_stages}
    flye_params = (plan.method_parameters or {}).get("assembly.candidate.flye")
    if flye_params and "assembly" in local_stages:
        payload["assembly"] = {"flye_parameters": flye_params}
    quast_params = (plan.method_parameters or {}).get("assembly_review.eval.quast")
    if quast_params and "assembly_review" in local_stages:
        payload["assembly_review"] = {"quast_threads": quast_params.get("threads", 4)}
    RunConfig.model_validate(payload)
    return payload


def write_ask_run_config(plan: AskPlanState) -> Path:
    plan = _auto_fill_inferred_defaults(plan)
    payload = build_run_config_payload(plan)
    path = Path(plan.data_dir) / "run_configs" / plan.session_id / "ask_run_config.json"
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
            executable = [m["method_name"] for m in methods if m.get("execution_support") == "executable"]
            selected[stage] = executable[0] if executable else (methods[0]["method_name"] if methods else "")
        return {"kind": "method_selection", "selected_methods": selected}
    if request.kind == "parameter_values":
        return {"kind": "parameter_values", "method_parameters": dict(request.choices.get("defaults", {}))}
    return {"kind": "confirm", "confirmed": True}


def _stage_selection_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    available = _available_stages_from_manifest(plan.manifest_ref)
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
    for stage in plan.resolved_execution_order or []:
        if stage not in _STAGES_NEEDING_METHOD_SELECTION:
            continue
        stage_methods = _STAGE_METHODS.get(stage, [])
        if stage_methods:
            methods_by_stage[stage] = [
                {"method_name": m["method_name"], "execution_support": m["execution_support"]}
                for m in stage_methods
            ]
    return AskClarificationRequest(
        status="clarification_required",
        kind="method_selection",
        session_id=plan.session_id,
        ask_plan_ref=ask_plan_ref,
        manifest_ref=plan.manifest_ref,
        question="Which methods should run for the selected stages?",
        choices={"methods_by_stage": methods_by_stage},
        required_fields=["selected_methods"],
        response_schema={"kind": "method_selection", "selected_methods": {stage: "" for stage in methods_by_stage}},
        next_action=_next_action(ask_plan_ref),
    )


def _parameter_values_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    missing_fields, defaults, warnings = _missing_parameters_and_defaults(plan)
    all_fields: list[str] = []
    for stage, method in _iter_selected_methods(plan):
        cap = _get_method_descriptor(method)
        if not cap or cap["execution_support"] != "executable":
            continue
        for param in cap["required_parameters"]:
            all_fields.append(f"{method}.{param}")
    display_fields = all_fields if all_fields else missing_fields
    choices: dict[str, Any] = {"defaults": defaults}
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
        response_schema={"kind": "parameter_values", "method_parameters": defaults},
        next_action=_next_action(ask_plan_ref),
    )


def _confirm_request(plan: AskPlanState, ask_plan_ref: str) -> AskClarificationRequest:
    summary = _build_run_plan_summary(plan)
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


def _build_run_plan_summary(plan: AskPlanState) -> dict[str, Any]:
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
    if plan.selected_methods:
        summary["selected_methods"] = plan.selected_methods
    if plan.method_parameters:
        summary["method_parameters"] = plan.method_parameters
    _, _, warnings = _missing_parameters_and_defaults(plan)
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


def _available_stages_from_manifest(manifest_ref: str) -> list[str]:
    manifest = _load_manifest(manifest_ref)
    if manifest is None:
        return []
    asset_types = {asset.asset_type for asset in manifest.assets}
    available = []
    if "raw_long_reads" in asset_types:
        available.extend(["read_qc", "genome_survey", "assembly", "assembly_review"])
    if "assembly_fasta_registered" in asset_types:
        available.extend(["assembly_review"])
    if "hic_reads_registered" in asset_types or "hic_alignment_bam" in asset_types:
        available.append("hic_scaffolding")
    if "repeat_library" in asset_types:
        available.append("repeat_annotation")
    return list(dict.fromkeys(available))


def _suggested_stages(plan: AskPlanState, available: list[str]) -> list[str]:
    if "assembly" in available:
        return ["read_qc", "assembly", "assembly_review"]
    return available[:2] if available else []


def _resolve_execution_order(selected: list[str], plan: AskPlanState) -> list[str]:
    manifest = _load_manifest(plan.manifest_ref)
    manifest_asset_types = {asset.asset_type for asset in manifest.assets} if manifest else set()
    required = set(selected)
    for stage in selected:
        deps = _ASSEMBLY_UPSTREAM_DEPS.get(stage, [])
        for dep in deps:
            if dep in required:
                continue
            if _can_skip_dependency(dep, manifest_asset_types):
                continue
            required.add(dep)
    return [stage for stage in STAGE_ORDER if stage in required]


def _can_skip_dependency(dep_stage: str, manifest_asset_types: set[str]) -> bool:
    if dep_stage == "read_qc" and "clean_long_reads" in manifest_asset_types:
        return True
    if dep_stage in ("assembly", "assembly_review") and "assembly_fasta_registered" in manifest_asset_types:
        return True
    return False


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
        for method in method_list:
            if not method or not method.strip():
                raise ValueError(f"empty method name for stage {stage!r}")
            if method not in known:
                stage_methods = [m["method_name"] for m in _STAGE_METHODS.get(stage, [])]
                raise ValueError(
                    f"unknown method {method!r} for stage {stage!r}; "
                    f"known methods: {sorted(stage_methods)}"
                )


def _needs_parameter_values(plan: AskPlanState) -> bool:
    """True when there are executable methods with parameters to show/confirm."""
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
    return False


def _auto_fill_inferred_defaults(plan: AskPlanState) -> AskPlanState:
    """Merge inferred defaults into method_parameters for any missing keys."""
    _, defaults, _ = _missing_parameters_and_defaults(plan)
    if not defaults:
        return plan
    return plan.model_copy(update={"method_parameters": defaults})


def _missing_parameters_and_defaults(plan: AskPlanState) -> tuple[list[str], dict[str, dict[str, Any]], list[str]]:
    missing_fields: list[str] = []
    defaults: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
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
    return missing_fields, defaults, warnings


def _infer_parameter_defaults(plan: AskPlanState, method: str, cap: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    inferred = dict(cap.get("parameter_defaults") or {})
    warnings: list[str] = []
    if method == "assembly.candidate.flye":
        manifest = _load_manifest(plan.manifest_ref)
        if manifest:
            platforms = {
                asset.platform for asset in manifest.assets
                if asset.asset_type == "raw_long_reads" and asset.platform != "unknown"
            }
            for platform, read_type in _FLYE_READ_TYPE_MAP.items():
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
    return inferred, warnings


def _stage_has_executable_method(plan: AskPlanState, stage: str) -> bool:
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
