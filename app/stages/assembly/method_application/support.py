"""Assembly-specific preparation and collection helpers.

The stage-neutral freeze/run/collect lifecycle lives under ``app.execution``.
This module only adapts Assembly parameter artifacts and candidate publication
to that lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from app.schemas.state import TopLevelState
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
    load_recommendation_bundle,
    write_attempt_json_artifact,
)
from app.stages.base import (
    absolute_data_dir,
    ordered_unique,
    stage_output,
    update_stage_output,
)
from app.storage.artifacts import (
    publish_artifact_record,
    publish_or_recover_work_artifact,
    register_artifact_records,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


def fail_update(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(
        state,
        "assembly",
        {"status": "failed", "reason": reason},
    )


def freeze_parameterized_attempt(
    state: TopLevelState,
    output: dict[str, Any],
    lifecycle: MethodExecutionLifecycle,
    *,
    label: str,
    approved_attempt: ParameterAttempt,
    script_path: Path,
    working_dir: Path,
    expected_outputs: list[Path],
    env_spec: Any,
    command_payload: dict[str, Any],
    frozen_parameters_payload: dict[str, Any],
    frozen_attempt_metadata: dict[str, Any],
    stage_updates: dict[str, Any],
) -> dict:
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
            "schema_version": 2,
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            **frozen_parameters_payload,
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
        env=output.get("env"),
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
    output: dict[str, Any],
    *,
    method_id: str,
    label: str,
) -> tuple[ParameterAttempt | None, dict[str, Any], str | None]:
    """Load the exact recommendation approved by the human parameter gate."""

    try:
        attempt = current_parameter_attempt(
            output,
            method_id=method_id,
            label=label,
        )
        if attempt.status != "recommended":
            raise ParameterAttemptStateError(
                f"{label} current parameter recommendation is not approved for freeze"
            )
        bundle = load_recommendation_bundle(state, attempt.recommendation_manifest_ref)
    except (ParameterAttemptStateError, ParameterPersistenceError) as exc:
        return None, {}, f"{label} recommendation evidence is invalid: {exc}"
    recommendation = dict(bundle["recommendation"].get("recommendation") or {})
    return attempt, dict(recommendation.get("parameters") or {}), None


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    *,
    execution: MethodExecution,
    substep_name: str,
    extra_artifacts: list[tuple[Path, str]],
    bundle_logs: dict[str, str],
    bundle_metadata: dict[str, Any],
) -> dict:
    """Register standard artifacts and materialize the selected candidate bundle."""
    output = stage_output(state, "assembly")
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = execution.attempt
    result = execution.evidence
    draft_value = execution.attempt.metadata.get("draft_assembly_path")
    if not isinstance(draft_value, str) or not draft_value:
        raise ValueError(
            f"{lifecycle.name} frozen attempt has no draft assembly path"
        )
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
        return update_stage_output(
            state,
            "assembly",
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
    return update_stage_output(
        state,
        "assembly",
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
) -> dict[str, Any]:
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


def _candidate_bundle_path(output: dict[str, Any], method_name: str) -> Path | None:
    for ref in output.get("selected_candidate_refs") or []:
        path = Path(ref)
        if (
            path.exists()
            and json.loads(path.read_text(encoding="utf-8")).get("method_name")
            == method_name
        ):
            return path
    return None
