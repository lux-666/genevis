"""Assembly-specific preparation and collection helpers.

The stage-neutral freeze/run/collect lifecycle lives under ``app.execution``.
This module only adapts Assembly parameter artifacts and candidate publication
to that lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
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
    AssemblyCandidateBundle,
    ParameterAttempt,
)
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    ResolvedParameterSources,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly.parameters.storage import (
    write_attempt_json_artifact,
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
    register_artifact_records,
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
    try:
        parameter_sources = ResolvedParameterSources.model_validate(
            frozen_parameters_payload.get("parameter_sources")
        )
    except ValueError as exc:
        return fail_update(
            state,
            f"{label} frozen parameter sources are invalid: {exc}",
        )
    command_ref = write_attempt_json_artifact(
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
    )
    frozen_parameters_ref = write_attempt_json_artifact(
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
        metadata={
            **frozen_attempt_metadata,
            "parameter_attempt_id": parameter_attempt_id,
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
    extra_artifacts: list[tuple[Path, str]],
    bundle_logs: dict[str, str],
    bundle_metadata: dict[str, Any],
) -> StateUpdate:
    """Register standard artifacts and materialize the selected candidate bundle."""
    output = assembly_stage_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = execution.attempt
    result = execution.evidence
    draft_value = execution.attempt.metadata.get("draft_assembly_path")
    if not isinstance(draft_value, str) or not draft_value:
        raise ValueError(f"{lifecycle.name} frozen attempt has no draft assembly path")
    draft_assembly = Path(draft_value)
    if not draft_assembly.exists():
        raise FileNotFoundError(
            f"expected {lifecycle.name} draft assembly missing: {draft_assembly}"
        )

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "candidate_id": f"{attempt.session_id}:{lifecycle.method_name}",
            },
        )
    ]
    paths = SessionPaths.from_run_dir(absolute_data_dir(state))
    for path, artifact_type in extra_artifacts:
        resolved_path = path.expanduser().resolve(strict=False)
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
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            except FileNotFoundError:
                continue
            resolved_path = published.path
        artifacts.append(
            ArtifactMeta.from_file(
                resolved_path,
                stage="assembly",
                method_name=lifecycle.method_name,
                artifact_type=artifact_type,
                metadata={"attempt_id": attempt.attempt_id},
            )
        )

    bundle_path = _candidate_bundle_path(output, lifecycle.method_name)
    if bundle_path is None:
        return update_assembly_stage_output(
            state,
            {
                "status": "failed",
                "reason": f"missing {lifecycle.name} candidate bundle ref",
            },
        )
    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_data["status"] = "executed"
    bundle_data["expected_outputs"] = {
        **dict(bundle_data.get("expected_outputs") or {}),
        "draft_assembly_fasta": str(draft_assembly),
    }
    bundle_data["logs"] = {
        **dict(bundle_data.get("logs") or {}),
        "stdout": result.stdout_ref.path_ref,
        "stderr": result.stderr_ref.path_ref,
        **bundle_logs,
    }
    bundle_data["metadata"] = {
        **dict(bundle_data.get("metadata") or {}),
        "execution_enabled": True,
        "attempt_id": attempt.attempt_id,
        "draft_assembly_artifact_ref": str(draft_assembly),
        "draft_assembly_ref_status": "materialized",
        **bundle_metadata,
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
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
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
        },
    )
    try:
        publish_artifact_record(
            state.get("data_dir", "data"),
            attempt.session_id,
            bundle_artifact,
            destination=executed_bundle_path,
        )
    finally:
        temporary_bundle.unlink(missing_ok=True)
    artifacts.append(bundle_artifact)

    register_artifact_records(
        state.get("data_dir", "data"), attempt.session_id, artifacts
    )
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(
        attempt.session_id, attempt, artifacts=artifact_payloads
    )
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(
            attempt.session_id, artifact, attempt_id=attempt.attempt_id
        )

    substeps = dict(output.get("substeps") or {})
    substeps[substep_name] = {
        "name": substep_name,
        "status": "executed",
        "method_name": lifecycle.method_name,
        "draft_assembly_ref": str(draft_assembly),
        "candidate_ref": str(executed_bundle_path),
    }
    return update_assembly_stage_output(
        state,
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(draft_assembly),
            "candidate_assembly_refs": [str(draft_assembly)],
            "executed_candidate_refs": [str(executed_bundle_path)],
            "artifact_refs": ordered_unique(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(item.file_path) for item in artifacts],
                ]
            ),
            "candidate_artifact_refs": ordered_unique(
                [
                    *list(output.get("candidate_artifact_refs") or []),
                    str(executed_bundle_path),
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


def _candidate_bundle_path(
    output: AssemblyStageCursor, method_name: str
) -> Path | None:
    for ref in output.get("selected_candidate_refs") or []:
        path = Path(ref)
        if (
            path.exists()
            and json.loads(path.read_text(encoding="utf-8")).get("method_name")
            == method_name
        ):
            return path
    return None
