"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import ArtifactMeta, AssemblyCandidateBundle
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.plan import AssemblyCandidatePlan
from app.schemas.state import TopLevelState

ASSEMBLY_AUTO_CANDIDATES = [
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
]
ASSEMBLY_COMPATIBILITY_CANDIDATES = ["assembly.candidate.hifiasm"]
ASSEMBLY_CANDIDATES = [*ASSEMBLY_AUTO_CANDIDATES, *ASSEMBLY_COMPATIBILITY_CANDIDATES]
GENOME_SIZE_REQUIRED = {
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
}
ASSEMBLY_CONTRACT_METHOD = "assembly.candidate_plan"


def _assembly_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("assembly", {}))


def _update_assembly_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {**_assembly_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_assembly_output(state, {"status": "failed", "reason": reason})


def enter_assembly(state: TopLevelState) -> dict:
    """Mark assembly as running."""
    return {"supervision_state": "running_stage"}


def resolve_assembly_inputs(state: TopLevelState) -> dict:
    """Resolve the stable long-read input for assembly planning."""
    input_ref = _read_qc_assembly_input_ref(state)
    input_type = "long_reads.fofn" if input_ref else None
    source = "read_qc"

    if not input_ref:
        input_ref = _asset_input_ref(state)
        input_type = "clean_long_reads" if input_ref else None
        source = "asset_registry"

    if not input_ref:
        return _fail_update(state, "assembly requires clean_long_reads or long_reads.fofn")

    genome_size_prior = _genome_size_prior(state)
    return _update_assembly_output(
        state,
        {
            "status": "resolved",
            "input_ref": input_ref,
            "input_type": input_type,
            "input_source": source,
            "genome_size_prior": genome_size_prior,
            "genome_survey_result_ref": _genome_survey_result_ref(state),
        },
    )


def build_assembly_candidate_set(state: TopLevelState) -> dict:
    """Build compatible and rejected assembly candidates from descriptors."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "resolved":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    planned_methods = _planned_candidate_methods(assembly_out)
    missing = [method for method in planned_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"assembly descriptors missing: {missing}")

    genome_size_prior = assembly_out.get("genome_size_prior")
    candidate_plans: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for method_name in planned_methods:
        requires_genome_size = method_name in GENOME_SIZE_REQUIRED
        rejection_reason = "genome_size_required" if requires_genome_size and not genome_size_prior else None
        status = "rejected" if rejection_reason else "compatible"
        if rejection_reason:
            rejected[method_name] = rejection_reason
        candidate_plans.append(
            AssemblyCandidatePlan(
                method_name=method_name,
                status=status,
                required_inputs=["long_reads"],
                optional_inputs=["genome_survey_result", "genome_size_prior"],
                requires_genome_size=requires_genome_size,
                rejection_reason=rejection_reason,
            ).model_dump(mode="json")
        )

    return _update_assembly_output(
        state,
        {
            "status": "candidate_set_ready",
            "candidate_methods": planned_methods,
            "candidate_plans": candidate_plans,
            "rejected_candidates": rejected,
        },
    )


def _planned_candidate_methods(assembly_out: dict[str, Any]) -> list[str]:
    decision = dict(assembly_out.get("method_decision") or {})
    methods = list(ASSEMBLY_AUTO_CANDIDATES)
    for key in ["primary", "fallback"]:
        method = decision.get(key)
        if method in ASSEMBLY_COMPATIBILITY_CANDIDATES and method not in methods:
            methods.append(method)
    return methods


def select_primary_and_fallback(state: TopLevelState) -> dict:
    """Select one primary and at most one fallback candidate."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "candidate_set_ready":
        return {}

    compatible = [
        candidate["method_name"]
        for candidate in assembly_out.get("candidate_plans", [])
        if candidate.get("status") == "compatible"
    ]
    decision = dict(assembly_out.get("method_decision") or {})
    requested_primary = decision.get("primary")
    requested_fallback = decision.get("fallback")

    if requested_primary:
        if requested_primary not in ASSEMBLY_CANDIDATES:
            return _block_for_human(state, f"unknown_assembly_method: {requested_primary}")
        if requested_primary not in compatible:
            reason = assembly_out.get("rejected_candidates", {}).get(requested_primary, "method_not_compatible")
            return _block_for_human(state, reason)
        primary = requested_primary
    elif compatible:
        primary = compatible[0]
    else:
        return _block_for_human(state, "no_compatible_assembly_candidate")

    fallback = None
    if requested_fallback:
        if requested_fallback not in ASSEMBLY_CANDIDATES:
            return _block_for_human(state, f"unknown_assembly_method: {requested_fallback}")
        if requested_fallback not in compatible:
            return _block_for_human(
                state,
                assembly_out.get("rejected_candidates", {}).get(requested_fallback, "method_not_compatible"),
            )
        if requested_fallback != primary:
            fallback = requested_fallback
    else:
        fallback = next((method for method in compatible if method != primary), None)

    selected = [primary, *([fallback] if fallback else [])][:2]
    return _update_assembly_output(
        state,
        {
            "status": "selected",
            "primary_method": primary,
            "fallback_method": fallback,
            "selected_methods": selected,
        },
    )


def plan_candidate_attempts(state: TopLevelState) -> dict:
    """Write planned candidate assembly bundles without running tools."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "selected":
        return {}

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    candidate_dir = data_dir / "artifacts" / "assembly" / session_id / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    candidate_refs: list[str] = []
    artifacts: list[ArtifactMeta] = []
    selected_plans = []
    for method_name in assembly_out["selected_methods"]:
        short_name = method_name.rsplit(".", 1)[-1]
        fasta_path = data_dir / "work" / "assembly" / session_id / short_name / "draft_assembly.fasta"
        log_path = data_dir / "work" / "assembly" / session_id / short_name / "assembly.log"
        bundle_path = candidate_dir / f"{short_name}_candidate.json"
        bundle = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{session_id}:{method_name}",
            method_name=method_name,
            status="planned",
            input_ref=assembly_out["input_ref"],
            input_type=assembly_out["input_type"],
            genome_size_prior=assembly_out.get("genome_size_prior"),
            expected_outputs={"draft_assembly_fasta": str(fasta_path)},
            logs={"primary_log": str(log_path)},
            metadata={
                "genome_survey_result_ref": assembly_out.get("genome_survey_result_ref"),
                "execution_enabled": False,
            },
        )
        bundle_path.write_text(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidate_refs.append(str(bundle_path))
        artifact = ArtifactMeta.from_file(
            bundle_path,
            stage="assembly",
            method_name=method_name,
            artifact_type="draft_assembly.candidate",
            metadata={"candidate_id": bundle.candidate_id, "execution_enabled": False},
        )
        artifacts.append(artifact)
        selected_plans.append(
            AssemblyCandidatePlan(
                method_name=method_name,
                status="selected",
                role="primary" if method_name == assembly_out["primary_method"] else "fallback",
                required_inputs=["long_reads"],
                optional_inputs=["genome_survey_result", "genome_size_prior"],
                requires_genome_size=method_name in GENOME_SIZE_REQUIRED,
                candidate_ref=str(bundle_path),
            ).model_dump(mode="json")
        )

    append_artifact_records(data_dir, session_id, artifacts)
    return _update_assembly_output(
        state,
        {
            "status": "planned",
            "selected_candidate_refs": candidate_refs,
            "candidate_artifact_refs": [str(artifact.file_path) for artifact in artifacts],
            "selected_candidate_plans": selected_plans,
        },
    )


def commit_assembly_plan(state: TopLevelState) -> dict:
    """Route planned candidate assembly refs to assembly_review."""
    trace = [*state.get("route_trace", []), "assembly_stage"]
    return {
        "current_stage": next_stage_name("assembly"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def fail_assembly(state: TopLevelState) -> dict:
    """Stop workflow on assembly planning failure."""
    assembly_out = _assembly_output(state)
    return {
        "current_stage": "assembly",
        "supervision_state": "failed_terminal",
        "last_error": assembly_out.get("reason", "assembly failed"),
    }


def block_assembly_for_human(state: TopLevelState) -> dict:
    """Pause workflow for a human assembly planning decision."""
    assembly_out = _assembly_output(state)
    reason = assembly_out.get("block_reason", "assembly decision required")
    return {
        "current_stage": "assembly",
        "supervision_state": "blocked_human",
        "active_interrupt": {
            "stage": "assembly",
            "reason": reason,
            "candidate_methods": assembly_out.get("candidate_methods", []),
            "rejected_candidates": assembly_out.get("rejected_candidates", {}),
        },
    }


def _block_for_human(state: TopLevelState, reason: str) -> dict:
    return _update_assembly_output(state, {"status": "blocked_human", "block_reason": reason})


def _read_qc_assembly_input_ref(state: TopLevelState) -> str | None:
    read_qc_out = (state.get("stage_outputs") or {}).get("read_qc") or {}
    for key in ["assembly_input_ref", "filtered_long_reads_fofn_ref", "long_reads_fofn_ref"]:
        value = read_qc_out.get(key)
        if value:
            return str(value)
    return None


def _asset_input_ref(state: TopLevelState) -> str | None:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return None
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return None
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if asset.get("asset_type") == "clean_long_reads":
            return str(asset.get("path"))
    return None


def _genome_size_prior(state: TopLevelState) -> int | None:
    assembly_out = _assembly_output(state)
    decision = dict(assembly_out.get("method_decision") or {})
    if decision.get("genome_size_prior"):
        return int(decision["genome_size_prior"])
    survey = (state.get("stage_outputs") or {}).get("genome_survey") or {}
    estimates = dict(survey.get("estimates") or {})
    for key in ["genome_size_prior", "genome_size"]:
        if estimates.get(key):
            return int(estimates[key])
    inventory = (state.get("stage_outputs") or {}).get("inventory") or {}
    if inventory.get("genome_size_estimate"):
        return int(inventory["genome_size_estimate"])
    return None


def _genome_survey_result_ref(state: TopLevelState) -> str | None:
    survey = (state.get("stage_outputs") or {}).get("genome_survey") or {}
    value = survey.get("result_artifact_ref")
    return str(value) if value else None
