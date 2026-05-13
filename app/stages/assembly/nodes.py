"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import attach_conda_fields, resolve_method_env
from app.execution.local_runner import LocalRunner
from app.graph.routing import next_stage_name
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
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
FLYE_METHOD = "assembly.candidate.flye"


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
    """Write candidate bundles and select the optional local Flye route."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "selected":
        return {}

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    candidate_dir = data_dir / "artifacts" / "assembly" / session_id / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    local_flye = assembly_out.get("execution_mode") == "local" and assembly_out.get("primary_method") == FLYE_METHOD
    candidate_refs: list[str] = []
    artifacts: list[ArtifactMeta] = []
    selected_plans = []
    for method_name in assembly_out["selected_methods"]:
        short_name = method_name.rsplit(".", 1)[-1]
        fasta_path = data_dir / "artifacts" / "assembly" / session_id / short_name / "draft_assembly.fasta"
        log_path = data_dir / "work" / "assembly" / session_id / short_name / "flye_out" / "flye.log"
        bundle_path = candidate_dir / f"{short_name}_candidate.json"
        execution_enabled = local_flye and method_name == FLYE_METHOD
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
                "execution_enabled": execution_enabled,
                "execution_mode": "local" if execution_enabled else "planned_only",
                "draft_assembly_ref_status": "pending_local_execution" if execution_enabled else "planned_path_not_materialized",
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
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            metadata={"candidate_id": bundle.candidate_id, "execution_enabled": execution_enabled},
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
    status = "flye_execution_planned" if local_flye else "planned"
    return _update_assembly_output(
        state,
        {
            "status": status,
            "selected_candidate_refs": candidate_refs,
            "candidate_artifact_refs": [str(artifact.file_path) for artifact in artifacts],
            "selected_candidate_plans": selected_plans,
            "substeps": {
                **dict(assembly_out.get("substeps") or {}),
                "flye_assembly": {
                    "name": "flye_assembly",
                    "status": "planned" if local_flye else "skipped",
                    "method_name": FLYE_METHOD,
                    "skip_reason": None if local_flye else "local_execution_not_enabled",
                },
            },
        },
    )


def freeze_flye_attempt(state: TopLevelState) -> dict:
    """Freeze a local Flye attempt for the primary assembly candidate."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "flye_execution_planned":
        return {}

    descriptor_error = _validate_methods([FLYE_METHOD])
    if descriptor_error:
        return _fail_update(state, descriptor_error)

    read_paths, error = _flye_read_paths(assembly_out.get("input_ref"), assembly_out.get("input_type"))
    if error:
        return _fail_update(state, error)

    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    env_spec, blocked = resolve_method_env(
        assembly_out,
        stage="assembly",
        method_name=FLYE_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_assembly_output(state, blocked)

    working_dir = data_dir / "work" / "assembly" / session_id / "flye"
    flye_out_dir = working_dir / "flye_out"
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id / "flye"
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    draft_assembly = artifact_dir / "draft_assembly.fasta"
    script_path = working_dir / "run_flye.sh"
    threads, thread_error = _flye_threads(assembly_out)
    if thread_error:
        return _fail_update(state, thread_error)
    reads_arg = " ".join(shlex.quote(str(path)) for path in read_paths)
    script_path.write_text(
        "set -e\n"
        f"flye --nano-raw {reads_arg} --out-dir {shlex.quote(str(flye_out_dir))} --threads {threads}\n"
        f"test -f {shlex.quote(str(flye_out_dir / 'assembly.fasta'))}\n"
        f"cp {shlex.quote(str(flye_out_dir / 'assembly.fasta'))} {shlex.quote(str(draft_assembly))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt_kwargs: dict[str, Any] = {
        "attempt_id": f"{session_id}:assembly:flye",
        "session_id": session_id,
        "stage": "assembly",
        "method_name": FLYE_METHOD,
        "script_path": script_path,
        "working_dir": working_dir,
        "interpreter": "/bin/bash",
        "expected_outputs": [draft_assembly],
        "env": {str(k): str(v) for k, v in (assembly_out.get("env") or {}).items()},
        "metadata": {
            "input_ref": assembly_out.get("input_ref"),
            "input_type": assembly_out.get("input_type"),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": "nano-raw",
            "threads": threads,
            "flye_output_dir": str(flye_out_dir),
            "draft_assembly_path": str(draft_assembly),
        },
    }
    attach_conda_fields(env_spec, attempt_kwargs)
    attempt = FrozenAttemptSpec(**attempt_kwargs)
    return _update_assembly_output(
        state,
        {
            "status": "flye_frozen",
            "flye_frozen_attempt": attempt.model_dump(mode="json"),
            "flye_output_dir": str(flye_out_dir),
            "flye_draft_assembly_path": str(draft_assembly),
        },
    )


def run_flye_attempt(state: TopLevelState) -> dict:
    """Run the frozen Flye attempt locally."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "flye_frozen":
        return {}

    attempt = FrozenAttemptSpec.model_validate(assembly_out["flye_frozen_attempt"])
    ledger = Ledger(state.get("data_dir", "data"))
    ledger.create_session(attempt.session_id, state.get("input_manifest_ref", ""))
    ledger.record_attempt_started(attempt.session_id, attempt)

    result = LocalRunner().run(attempt)
    if not result.success:
        for path in attempt.expected_outputs:
            if path.exists() and path.is_file():
                path.unlink()
        stderr = result.stderr_path.read_text(encoding="utf-8") if result.stderr_path.exists() else ""
        error = stderr.strip() or f"flye failed with return code {result.return_code}"
        ledger.record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=result.return_code,
            error=error,
        )
        return _update_assembly_output(
            state,
            {
                "status": "failed",
                "reason": error,
                "flye_execution_result": result.model_dump(mode="json"),
            },
        )

    return _update_assembly_output(
        state,
        {
            "status": "flye_ran",
            "flye_execution_result": result.model_dump(mode="json"),
        },
    )


def collect_flye_artifacts(state: TopLevelState) -> dict:
    """Collect Flye draft assembly, command logs, and executed candidate bundle."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "flye_ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(assembly_out["flye_frozen_attempt"])
    result = assembly_out["flye_execution_result"]
    draft_assembly = Path(assembly_out["flye_draft_assembly_path"])
    if not draft_assembly.exists():
        reason = f"expected Flye draft assembly missing: {draft_assembly}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=0,
            error=reason,
        )
        return _update_assembly_output(state, {"status": "failed", "reason": reason})

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=FLYE_METHOD,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={"attempt_id": attempt.attempt_id, "candidate_id": f"{attempt.session_id}:{FLYE_METHOD}"},
        )
    ]
    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
        (Path(assembly_out["flye_output_dir"]) / "flye.log", "assembly.flye_log"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="assembly",
                    method_name=FLYE_METHOD,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    bundle_ref = _candidate_bundle_ref(assembly_out, FLYE_METHOD)
    if not bundle_ref:
        return _update_assembly_output(state, {"status": "failed", "reason": "missing Flye candidate bundle ref"})
    bundle_path = Path(bundle_ref)
    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_data["status"] = "executed"
    bundle_data["expected_outputs"] = {
        **dict(bundle_data.get("expected_outputs") or {}),
        "draft_assembly_fasta": str(draft_assembly),
    }
    bundle_data["logs"] = {
        **dict(bundle_data.get("logs") or {}),
        "stdout": str(result["stdout_path"]),
        "stderr": str(result["stderr_path"]),
    }
    bundle_data["metadata"] = {
        **dict(bundle_data.get("metadata") or {}),
        "execution_enabled": True,
        "execution_mode": "local",
        "attempt_id": attempt.attempt_id,
        "draft_assembly_artifact_ref": str(draft_assembly),
        "draft_assembly_ref_status": "materialized",
        "read_profile": "nano-raw",
    }
    bundle = AssemblyCandidateBundle.model_validate(bundle_data)
    bundle_path.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle_artifact = ArtifactMeta.from_file(
        bundle_path,
        stage="assembly",
        method_name=FLYE_METHOD,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={"candidate_id": bundle.candidate_id, "execution_enabled": True, "attempt_id": attempt.attempt_id},
    )
    artifacts.append(bundle_artifact)

    append_artifact_records(state.get("data_dir", "data"), attempt.session_id, artifacts)
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)

    substeps = dict(assembly_out.get("substeps") or {})
    substeps["flye_assembly"] = {
        "name": "flye_assembly",
        "status": "executed",
        "method_name": FLYE_METHOD,
        "draft_assembly_ref": str(draft_assembly),
        "candidate_ref": str(bundle_path),
    }
    return _update_assembly_output(
        state,
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(draft_assembly),
            "candidate_assembly_refs": [str(draft_assembly)],
            "executed_candidate_refs": [str(bundle_path)],
            "artifact_refs": _unique_refs([
                *list(assembly_out.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ]),
            "candidate_artifact_refs": _unique_refs([
                *list(assembly_out.get("candidate_artifact_refs") or []),
                str(bundle_path),
            ]),
            "substeps": substeps,
        },
    )


def commit_assembly_plan(state: TopLevelState) -> dict:
    """Route compact assembly candidate refs to assembly_review."""
    assembly_out = _assembly_output(state)
    committed_keys = [
        "artifact_refs",
        "candidate_artifact_refs",
        "candidate_assembly_refs",
        "candidate_methods",
        "candidate_plans",
        "draft_assembly_ref",
        "executed_candidate_refs",
        "fallback_method",
        "genome_size_prior",
        "genome_survey_result_ref",
        "input_ref",
        "input_source",
        "input_type",
        "primary_method",
        "rejected_candidates",
        "selected_candidate_plans",
        "selected_candidate_refs",
        "selected_methods",
        "status",
        "substeps",
    ]
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {key: assembly_out[key] for key in committed_keys if key in assembly_out}
    trace = [*state.get("route_trace", []), "assembly_stage"]
    return {
        "current_stage": next_stage_name("assembly"),
        "supervision_state": "running_stage",
        "route_trace": trace,
        "stage_outputs": outputs,
    }


def fail_assembly(state: TopLevelState) -> dict:
    """Stop workflow on assembly planning failure or preflight block."""
    assembly_out = _assembly_output(state)
    status = assembly_out.get("status", "failed")
    default_reason = {
        "blocked_env": "assembly blocked on missing conda environment",
        "missing_tool": "assembly blocked on missing tool",
    }.get(status, "assembly failed")
    return {
        "current_stage": "assembly",
        "supervision_state": "failed_terminal",
        "last_error": assembly_out.get("reason", default_reason),
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


def _validate_methods(methods: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in methods if method not in registry.methods]
    if missing:
        return f"assembly descriptors missing: {missing}"
    return None


def _candidate_bundle_ref(assembly_out: dict[str, Any], method_name: str) -> str | None:
    for ref in assembly_out.get("selected_candidate_refs") or []:
        path = Path(ref)
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("method_name") == method_name:
            return str(path)
    return None


def _flye_read_paths(input_ref: Any, input_type: Any) -> tuple[list[Path], str | None]:
    if not input_ref:
        return [], "Flye local execution requires assembly input_ref"
    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            return [], f"Flye local execution FOFN not found: {fofn}"
        paths = [Path(line.strip()) for line in fofn.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
    else:
        return [], f"Flye local execution does not support input_type: {input_type}"
    if not paths:
        return [], "Flye local execution requires at least one read path"
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        return [], f"Flye local execution read paths not found: {missing}"
    return paths, None


def _unique_refs(refs: list[str]) -> list[str]:
    unique = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


def _flye_threads(assembly_out: dict[str, Any]) -> tuple[int, str | None]:
    params = dict(assembly_out.get("flye_parameters") or {})
    raw_threads = params.get("threads", 1)
    try:
        threads = int(raw_threads)
    except (TypeError, ValueError):
        return 0, "assembly.flye_parameters.threads must be a positive integer"
    if threads <= 0:
        return 0, "assembly.flye_parameters.threads must be a positive integer"
    return threads, None


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
