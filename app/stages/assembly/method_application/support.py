"""Assembly-specific preparation and collection helpers.

The stage-neutral freeze/run/collect lifecycle lives under ``app.execution``.
This module only adapts Assembly parameter artifacts and candidate publication
to that lifecycle.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from app.domains.assembly.constants import (
    FLYE_METHOD,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)
from app.domains.assembly.selection import NormalizedReadType, method_read_type
from app.execution.attempt_owners import MethodExecution
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
)
from app.execution.lifecycle import MethodExecutionLifecycle, freeze_method_attempts
from app.execution.method_runner import build_frozen_attempt
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_PARAMETER_COMMAND,
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ArtifactMeta,
    ArtifactRef,
    ArtifactRefJson,
    AssemblyCandidateBundle,
    AssemblyNativeOutput,
    ParameterAttempt,
)
from app.schemas.execution import InputDependency
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    ResolvedParameterSources,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly.parameters.storage import (
    write_artifact as _write_attempt_artifact,
)
from app.stages.assembly.state import (
    AssemblyStageCursor,
    assembly_stage_output,
    update_assembly_stage_output,
)
from app.stages.base import (
    absolute_data_dir,
    ordered_unique,
)
from app.storage.artifacts import (
    publish_artifact_record,
    publish_or_recover_work_artifact,
    record_registered_artifacts,
    register_artifact_records,
)
from app.storage.assembly_candidates import (
    VerifiedPlannedCandidate,
    load_executed_candidate_bundle,
    load_planned_candidate_bundle,
)
from app.storage.ledger import Ledger
from app.storage.parameter_evidence import (
    ApprovedParameterBundleV3,
    ParameterEvidenceError,
    load_approved_parameter_chain_v3,
)
from app.storage.paths import SessionPaths

_ASSEMBLY_METHODS = frozenset(
    {
        FLYE_METHOD,
        NEXTDENOVO_METHOD,
        HIFIASM_METHOD,
        HIFIASM_ONT_METHOD,
    }
)


@dataclass(frozen=True, slots=True)
class AssemblyNativeOutputDeclaration:
    """Adapter-owned declaration of one optional method-native deliverable."""

    path: Path
    artifact_type: str
    artifact_class: Literal[
        "assembly_graph",
        "alternate_contigs",
        "haplotype_contigs",
        "report",
        "config",
        "log",
        "other",
    ]
    native_role: str
    media_type: str


_PARAMETER_KEY_BY_METHOD = {
    FLYE_METHOD: "flye_parameters",
    NEXTDENOVO_METHOD: "nextdenovo_parameters",
    HIFIASM_METHOD: "hifiasm_parameters",
    HIFIASM_ONT_METHOD: "hifiasm_ont_parameters",
}
ASSEMBLY_EXECUTION_CONTRACT_KEY = "assembly_execution_contract"


class AssemblyExecutionContractParameterError(ValueError):
    """Structured run-config validation failure at the contract producer."""

    def __init__(
        self,
        *,
        parameter_key: str,
        method_id: str,
        invalid: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            "invalid Assembly execution contract parameter: "
            + ", ".join(item["param"] for item in invalid)
        )
        self.parameter_key = parameter_key
        self.method_id = method_id
        self.invalid = invalid

    def stage_update(self) -> AssemblyStageCursor:
        return blocked_parameters(
            self.parameter_key,
            self.method_id,
            [],
            self.invalid,
        )


class AssemblyExecutionEnvironment(BaseModel):
    """Frozen process-environment overrides supplied by run configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["run_config"] = "run_config"
    values: dict[str, str] = Field(default_factory=dict)


class AssemblyThreadBinding(BaseModel):
    """One explicit L0 thread value and its producer provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: PositiveInt
    source: Literal["run_config"] = "run_config"


class AssemblyExecutionContract(BaseModel):
    """Typed producer-to-freeze contract for one Assembly method attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
    method_id: str
    environment: AssemblyExecutionEnvironment
    threads: AssemblyThreadBinding | None = None

    @model_validator(mode="after")
    def _known_method(self) -> "AssemblyExecutionContract":
        if self.method_id not in _ASSEMBLY_METHODS:
            raise ValueError(f"unsupported Assembly method: {self.method_id}")
        return self

    def l0_executor_values(self) -> dict[str, int]:
        """Project only registered resolver bindings from the frozen contract."""

        if self.threads is None:
            return {}
        return {"assembly_threads": self.threads.value}


def build_assembly_execution_contract(
    output: AssemblyStageCursor,
    *,
    method_id: str,
) -> AssemblyExecutionContract:
    """Freeze run-config execution inputs at the Assembly planning boundary."""

    parameter_key = _PARAMETER_KEY_BY_METHOD.get(method_id)
    if parameter_key is None:
        raise ValueError(f"unsupported Assembly method: {method_id}")
    raw_env = output.get("env")
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        raise ValueError("Assembly run-config env must be a mapping")
    raw_parameters = output.get(parameter_key)
    if raw_parameters is None:
        raw_parameters = {}
    if not isinstance(raw_parameters, dict):
        raise ValueError(f"{parameter_key} must be a mapping")
    thread_value = raw_parameters.get("threads")
    threads = None
    if thread_value is not None:
        invalid: list[dict[str, Any]] = []
        threads = positive_int(
            thread_value,
            f"{parameter_key}.threads",
            [],
            invalid,
        )
        if invalid or threads is None:
            raise AssemblyExecutionContractParameterError(
                parameter_key=parameter_key,
                method_id=method_id,
                invalid=invalid,
            )
    return AssemblyExecutionContract(
        method_id=method_id,
        environment=AssemblyExecutionEnvironment(
            values={str(key): str(value) for key, value in raw_env.items()}
        ),
        threads=(AssemblyThreadBinding(value=threads) if threads is not None else None),
    )


def load_assembly_execution_contract(
    output: AssemblyStageCursor,
    *,
    method_id: str,
) -> AssemblyExecutionContract:
    """Validate the frozen execution contract without legacy-field fallback."""

    payload = output.get(ASSEMBLY_EXECUTION_CONTRACT_KEY)
    if payload is None:
        raise ValueError("Assembly schema-3 execution contract is missing")
    contract = AssemblyExecutionContract.model_validate(payload)
    if contract.method_id != method_id:
        raise ValueError(
            "Assembly execution contract method mismatch: "
            f"expected {method_id!r}, got {contract.method_id!r}"
        )
    return contract


def fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return update_assembly_stage_output(
        state,
        {"status": "failed", "reason": reason},
    )


def freeze_parameterized_attempt(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    *,
    label: str,
    execution_contract: AssemblyExecutionContract,
    approved_attempt: ParameterAttempt,
    script_path: Path,
    working_dir: Path,
    expected_outputs: list[Path],
    env_spec: Any,
    input_dependencies: list[InputDependency],
    command_payload: dict[str, Any],
    frozen_parameters_payload: dict[str, Any],
    frozen_attempt_metadata: dict[str, Any],
    stage_updates: dict[str, Any],
) -> StateUpdate:
    """Persist and attach one human-approved Assembly execution attempt."""

    if (
        approved_attempt.method_id != lifecycle.method_name
        or approved_attempt.status != "recommended"
    ):
        return fail_update(
            state,
            f"{label} current parameter recommendation is not approved for freeze",
        )
    parameter_attempt_id = approved_attempt.attempt_id
    output = assembly_stage_output(state)
    try:
        planned_candidate = _planned_candidate_bundle(
            state,
            output,
            lifecycle.method_name,
            parameter_attempt_id=parameter_attempt_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        return fail_update(
            state,
            f"{label} planned candidate bundle is invalid: {exc}",
        )
    planned_candidate_ref = planned_candidate.ref
    try:
        parameter_sources = ResolvedParameterSources.model_validate(
            frozen_parameters_payload.get("parameter_sources")
        )
    except ValueError as exc:
        return fail_update(
            state,
            f"{label} frozen parameter sources are invalid: {exc}",
        )
    command_ref = _write_attempt_artifact(
        state,
        parameter_attempt_id,
        "command.json",
        {
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            **command_payload,
        },
        artifact_type=ASSEMBLY_PARAMETER_COMMAND,
        method_name=lifecycle.method_name,
        scope="attempt",
    )
    frozen_parameters_ref = _write_attempt_artifact(
        state,
        parameter_attempt_id,
        "frozen_parameters.json",
        {
            "schema_version": 3,
            "stage": "assembly",
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            "recommendation_manifest_ref": (
                approved_attempt.recommendation_manifest_ref.model_dump(mode="json")
            ),
            "approval_decision_ref": approved_attempt.approval_decision_ref.model_dump(
                mode="json"
            ),
            **parameter_sources.model_dump(mode="json"),
        },
        artifact_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
        method_name=lifecycle.method_name,
        scope="attempt",
    )
    session_id = state["session_id"]
    attempt = build_frozen_attempt(
        attempt_id=(
            f"{session_id}:assembly:{lifecycle.name}:"
            f"parameter_attempt_{parameter_attempt_id}"
        ),
        session_id=session_id,
        stage="assembly",
        method_name=lifecycle.method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=expected_outputs,
        env=execution_contract.environment.values,
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={
            **frozen_attempt_metadata,
            "parameter_attempt_id": parameter_attempt_id,
            "planned_candidate_bundle_ref": planned_candidate_ref.model_dump(
                mode="json"
            ),
        },
    )
    return freeze_method_attempts(
        state,
        lifecycle,
        [attempt],
        owner_metadata={
            "frozen_parameters_ref": frozen_parameters_ref.model_dump(mode="json"),
            "command_ref": command_ref.model_dump(mode="json"),
        },
        stage_updates=stage_updates,
    )


def load_current_approved_recommendation(
    state: TopLevelState,
    output: AssemblyStageCursor,
    *,
    method_id: str,
    label: str,
) -> tuple[ApprovedParameterBundleV3 | None, str | None]:
    """Load the exact immutable evidence chain approved for execution."""

    try:
        attempt = current_parameter_attempt(
            output,
            method_id=method_id,
            label=label,
        )
        bundle = load_approved_parameter_chain_v3(
            state,
            attempt=attempt,
            expected_stage="assembly",
            expected_method_id=method_id,
        )
    except (ParameterAttemptStateError, ParameterEvidenceError) as exc:
        return None, f"{label} recommendation evidence is invalid: {exc}"
    return bundle, None


def approved_method_read_type(
    bundle: ApprovedParameterBundleV3,
    *,
    method_id: str,
) -> str:
    """Map the persisted Assembly snapshot read type to one method value."""

    if method_id not in _ASSEMBLY_METHODS:
        raise ValueError(f"unsupported Assembly method: {method_id}")
    read_type = approved_assembly_read_type(bundle)
    mapped = method_read_type(method_id, read_type)
    if mapped is None:
        raise ValueError(
            f"approved Assembly read type {read_type!r} is incompatible "
            f"with method {method_id!r}"
        )
    return mapped


def approved_assembly_read_type(
    bundle: ApprovedParameterBundleV3,
) -> NormalizedReadType:
    """Return the canonical read type persisted in an Assembly snapshot."""

    context = bundle.chain.input_snapshot.stage_context
    if not isinstance(context, AssemblyRecommendationContext):
        raise ValueError("approved Assembly snapshot has the wrong stage context")
    return context.read_type


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    *,
    execution: MethodExecution,
    substep_name: str,
    native_outputs: list[AssemblyNativeOutputDeclaration] | None = None,
) -> StateUpdate:
    """Publish one executed candidate bundle without mutating its plan."""
    output = assembly_stage_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = execution.attempt
    result = execution.evidence
    parameter_attempt_id = attempt.metadata.get("parameter_attempt_id")
    if not isinstance(parameter_attempt_id, int) or parameter_attempt_id < 0:
        raise ValueError(
            f"{lifecycle.name} frozen attempt has no parameter attempt identity"
        )
    planned_candidate = _planned_candidate_bundle(
        state,
        output,
        lifecycle.method_name,
        parameter_attempt_id=parameter_attempt_id,
    )
    plan_ref = planned_candidate.ref
    attempt_plan_ref = ArtifactRef.model_validate(
        attempt.metadata.get("planned_candidate_bundle_ref")
    )
    if attempt_plan_ref != plan_ref:
        raise ValueError(f"{lifecycle.name} frozen attempt candidate plan ref drifted")
    bundle_path = planned_candidate.path
    plan = planned_candidate.bundle
    draft_value = execution.attempt.metadata.get("draft_assembly_path")
    if not isinstance(draft_value, str) or not draft_value:
        raise ValueError(f"{lifecycle.name} frozen attempt has no draft assembly path")
    draft_assembly = Path(draft_value)
    if not draft_assembly.is_file():
        raise FileNotFoundError(
            f"expected {lifecycle.name} draft assembly missing: {draft_assembly}"
        )
    if draft_assembly.stat().st_size <= 0:
        raise ValueError(
            f"expected {lifecycle.name} draft assembly is empty: {draft_assembly}"
        )

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "candidate_id": plan.candidate_id,
            },
        )
    ]
    paths = SessionPaths.from_run_dir(absolute_data_dir(state))
    artifact_paths: set[Path] = {draft_assembly.resolve(strict=False)}

    retained_native: list[tuple[AssemblyNativeOutputDeclaration, Path]] = []
    seen_native_roles: set[str] = set()
    for declaration in native_outputs or []:
        if declaration.native_role in seen_native_roles:
            raise ValueError(
                f"duplicate Assembly native role: {declaration.native_role}"
            )
        seen_native_roles.add(declaration.native_role)
        resolved_path = declaration.path.expanduser().resolve(strict=False)
        metadata = {
            "attempt_id": attempt.attempt_id,
            "candidate_id": plan.candidate_id,
            "native_role": declaration.native_role,
            "artifact_class": declaration.artifact_class,
        }
        try:
            resolved_path.relative_to(paths.artifacts_dir)
        except ValueError:
            try:
                published = publish_or_recover_work_artifact(
                    paths.run_dir,
                    attempt.session_id,
                    resolved_path,
                    stage="assembly",
                    method_name=lifecycle.method_name,
                    artifact_type=declaration.artifact_type,
                    metadata=metadata,
                )
            except FileNotFoundError:
                continue
            resolved_path = published.path
        if not resolved_path.is_file() or resolved_path.stat().st_size <= 0:
            continue
        if resolved_path in artifact_paths:
            raise ValueError(
                "one Assembly artifact path cannot own multiple output roles: "
                f"{resolved_path}"
            )
        artifact_paths.add(resolved_path)
        artifacts.append(
            ArtifactMeta.from_file(
                resolved_path,
                stage="assembly",
                method_name=lifecycle.method_name,
                artifact_type=declaration.artifact_type,
                metadata=metadata,
            )
        )
        retained_native.append((declaration, resolved_path))

    output_refs = register_artifact_records(
        state.get("data_dir", "data"), attempt.session_id, artifacts
    )
    ref_by_path = {
        Path(artifact.file_path).resolve(strict=False): ref
        for artifact, ref in zip(artifacts, output_refs, strict=True)
    }
    primary_output = ref_by_path[draft_assembly.resolve(strict=False)]
    native_entries = [
        AssemblyNativeOutput(
            artifact_class=declaration.artifact_class,
            native_role=declaration.native_role,
            media_type=declaration.media_type,
            artifact_ref=ref_by_path[path.resolve(strict=False)],
        )
        for declaration, path in retained_native
    ]
    log_refs = {
        "stdout": result.stdout_ref,
        "stderr": result.stderr_ref,
    }

    bundle_data = plan.model_dump(mode="json")
    bundle_data["status"] = "executed"
    bundle_data["plan_ref"] = plan_ref.model_dump(mode="json")
    bundle_data["primary_output"] = primary_output.model_dump(mode="json")
    bundle_data["native_outputs"] = [
        entry.model_dump(mode="json") for entry in native_entries
    ]
    bundle_data["log_refs"] = {
        name: ref.model_dump(mode="json") for name, ref in log_refs.items()
    }
    bundle = AssemblyCandidateBundle.model_validate(bundle_data)
    executed_bundle_path = bundle_path.with_name(
        bundle_path.name.replace(".planned.json", ".executed.json")
    )
    if executed_bundle_path == bundle_path:
        executed_bundle_path = bundle_path.with_name(
            f"{bundle_path.stem}.executed.json"
        )
    temporary_bundle = Path(attempt.working_dir) / (
        f".{executed_bundle_path.name}.{uuid4().hex}.tmp"
    )
    temporary_bundle.write_text(
        json.dumps(
            bundle.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    bundle_artifact = ArtifactMeta.from_file(
        temporary_bundle,
        stage="assembly",
        method_name=lifecycle.method_name,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": bundle.candidate_id,
            "execution_enabled": True,
            "attempt_id": attempt.attempt_id,
            "dependency_refs": [
                plan_ref.model_dump(mode="json"),
                primary_output.model_dump(mode="json"),
                *[
                    entry.artifact_ref.model_dump(mode="json")
                    for entry in native_entries
                ],
                *[ref.model_dump(mode="json") for ref in log_refs.values()],
            ],
        },
    )
    try:
        published_bundle = publish_artifact_record(
            state.get("data_dir", "data"),
            attempt.session_id,
            bundle_artifact,
            destination=executed_bundle_path,
        )
    finally:
        temporary_bundle.unlink(missing_ok=True)
    verified_bundle = load_executed_candidate_bundle(
        absolute_data_dir(state),
        attempt.session_id,
        published_bundle.ref,
        expected_method_name=lifecycle.method_name,
        expected_candidate_id=plan.candidate_id,
        expected_parameter_attempt_id=parameter_attempt_id,
    )
    if verified_bundle.primary_output_ref != primary_output:
        raise ValueError(
            f"{lifecycle.name} published candidate primary output ref drifted"
        )
    artifacts.append(bundle_artifact)
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(
        attempt.session_id, attempt, artifacts=artifact_payloads
    )
    record_registered_artifacts(
        state.get("data_dir", "data"),
        attempt.session_id,
        artifact_payloads,
        attempt_id=attempt.attempt_id,
    )

    substeps = dict(output.get("substeps") or {})
    substeps[substep_name] = {
        "name": substep_name,
        "status": "executed",
        "method_name": lifecycle.method_name,
        "draft_assembly_ref": str(verified_bundle.primary_output_path),
    }
    return update_assembly_stage_output(
        state,
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(verified_bundle.primary_output_path),
            "executed_candidate_bundle_ref": cast(
                ArtifactRefJson, verified_bundle.ref.model_dump(mode="json")
            ),
            "artifact_refs": ordered_unique(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(item.file_path) for item in artifacts],
                ]
            ),
            "substeps": substeps,
        },
    )


def positive_int(
    value: Any,
    name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> int | None:
    """Parse a positive integer while preserving structured validation errors."""
    if value is None or value == "":
        missing.append(name)
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        invalid.append(
            {"param": name, "value": value, "reason": "must be a positive integer"}
        )
        return None
    if converted <= 0:
        invalid.append(
            {"param": name, "value": value, "reason": "must be a positive integer"}
        )
    return converted


def blocked_parameters(
    prefix: str,
    method_name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> AssemblyStageCursor:
    """Create the shared blocked-param state payload."""
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if invalid:
        details.append("invalid: " + ", ".join(item["param"] for item in invalid))
    return {
        "status": "blocked_param",
        "reason": f"{prefix} incomplete ({'; '.join(details)})",
        "missing_params": missing,
        "invalid_params": invalid,
        "method_name": method_name,
    }


def build_read_input_dependencies(
    state: TopLevelState,
    output: Any,
    role: str,
    paths: Sequence[Any],
) -> list[InputDependency]:
    """Build verified frozen-input dependencies for one ordered read role."""
    member_ids: list[str] = list(
        (dict(output.get("assembly_input_member_ids") or {})).get(role) or []
    )
    artifact_refs: list[Any] = list(
        (dict(output.get("assembly_input_artifact_refs") or {})).get(role) or []
    )
    path_list = list(paths)
    if len(member_ids) != len(path_list):
        raise ValueError(
            f"input dependency cardinality mismatch for role {role!r}: "
            f"{len(member_ids)} member_ids vs {len(path_list)} paths"
        )
    if len(artifact_refs) != len(member_ids):
        raise ValueError(
            f"input dependency cardinality mismatch for role {role!r}: "
            f"{len(artifact_refs)} artifact refs vs {len(member_ids)} member IDs"
        )
    deps: list[InputDependency] = []
    for member_id, art_ref, path in zip(
        member_ids,
        artifact_refs,
        path_list,
        strict=True,
    ):
        if art_ref is not None:
            deps.append(artifact_input_dependency(state, art_ref, path))
        else:
            deps.extend(asset_input_dependencies(state, [member_id], [path]))
    return deps


def _planned_candidate_bundle(
    state: TopLevelState,
    output: AssemblyStageCursor,
    method_name: str,
    *,
    parameter_attempt_id: int,
) -> VerifiedPlannedCandidate:
    """Resolve the one typed planned candidate and bind it to Assembly input."""

    raw_ref = output.get("planned_candidate_bundle_ref")
    if raw_ref is None:
        raise ValueError("planned candidate bundle ref is missing")
    verified = load_planned_candidate_bundle(
        absolute_data_dir(state),
        state["session_id"],
        ArtifactRef.model_validate(raw_ref),
        expected_method_name=method_name,
        expected_parameter_attempt_id=parameter_attempt_id,
    )
    expected_member_ids = dict(output.get("assembly_input_member_ids") or {})
    expected_platforms = dict(output.get("assembly_input_member_platforms") or {})
    if (
        verified.bundle.input_type != output.get("input_type")
        or verified.bundle.input_member_ids != expected_member_ids
        or verified.bundle.input_member_platforms != expected_platforms
    ):
        raise ValueError("planned candidate identity drifted from Assembly input")
    return verified
