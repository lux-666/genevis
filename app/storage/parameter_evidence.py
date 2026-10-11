"""Stage-neutral persistence for parameter-recommendation evidence.

Recommendation logic remains owned by each biological stage.  This module
owns safe run-local paths, immutable publication, artifact-index registration,
and the strict v3 recommendation-chain read boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast
from uuid import uuid4

from app.control.workflow_plan import current_workflow_step_id
from app.llm.parameters.assembly.request import validate_recorded_parameter_request
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    HIC_PARAMETER_APPROVAL_DECISION,
    HIC_PARAMETER_BASIS,
    HIC_PARAMETER_INPUT_SNAPSHOT,
    HIC_PARAMETER_MODEL_REQUEST,
    HIC_PARAMETER_MODEL_RESPONSE,
    HIC_PARAMETER_RECOMMENDATION,
    HIC_PARAMETER_RECOMMENDATION_MANIFEST,
    HIC_PARAMETER_RESPONSE_PARSE,
    T2T_PARAMETER_APPROVAL_DECISION,
    T2T_PARAMETER_BASIS,
    T2T_PARAMETER_INPUT_SNAPSHOT,
    T2T_PARAMETER_POLICY_REQUEST,
    T2T_PARAMETER_POLICY_RESPONSE,
    T2T_PARAMETER_RECOMMENDATION,
    T2T_PARAMETER_RECOMMENDATION_MANIFEST,
    T2T_PARAMETER_RESPONSE_PARSE,
    ArtifactMeta,
    ArtifactRef,
    ParameterAttempt,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    ParameterRecommendationManifestV3,
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
)
from app.schemas.state import TopLevelState
from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_artifact_record,
    read_artifact_records,
    recover_published_artifact,
    resolve_artifact_ref,
)
from app.storage.paths import SessionPaths

ParameterEvidenceScope = Literal["recommendation", "attempt"]


class ParameterEvidenceError(ValueError):
    """Raised when shared parameter evidence is unsafe or inconsistent."""


@dataclass(frozen=True)
class _ParameterEvidenceLocation:
    paths: SessionPaths
    session_id: str
    stage: str
    workflow_step_id: str
    destination: Path


@dataclass(frozen=True)
class ParameterRecommendationArtifactTypes:
    """Stage-owned artifact types participating in the shared v3 chain."""

    input_snapshot: str
    basis_snapshot: str
    model_request: str
    model_response: str
    response_parse: str
    recommendation: str
    recommendation_manifest: str
    approval_decision: str


@dataclass(frozen=True)
class ParameterRecommendationChainV3:
    """Integrity-checked records bound by one schema-3 manifest."""

    manifest_ref: ArtifactRef
    manifest: ParameterRecommendationManifestV3
    input_snapshot: RecommendationInputSnapshotV3
    basis_snapshot: dict[str, Any]
    catalog: MethodParameterCatalog
    model_request: dict[str, Any]
    model_response: dict[str, Any]
    response_parse: dict[str, Any]
    recommendation: dict[str, Any]


@dataclass(frozen=True)
class ApprovedParameterBundleV3:
    """The sole approved input accepted by parameterized execution adapters."""

    attempt: ParameterAttempt
    chain: ParameterRecommendationChainV3
    approval_decision: dict[str, Any]

    @property
    def approved_l2(self) -> dict[str, Any]:
        recommendation = self.chain.recommendation.get("recommendation")
        if not isinstance(recommendation, dict):
            raise ParameterEvidenceError(
                "approved recommendation has no recommendation mapping"
            )
        parameters = recommendation.get("parameters")
        if not isinstance(parameters, dict):
            raise ParameterEvidenceError(
                "approved recommendation has no parameters mapping"
            )
        if (
            self.chain.manifest.summary.get("policy") == "deterministic_baseline"
            and parameters
        ):
            raise ParameterEvidenceError(
                "deterministic baseline approval must not contain L2 mappings"
            )
        return dict(parameters)


_PARAMETER_ARTIFACT_TYPES: dict[
    Literal["assembly", "hic_scaffolding", "t2t"],
    ParameterRecommendationArtifactTypes,
] = {
    "assembly": ParameterRecommendationArtifactTypes(
        input_snapshot=ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
        basis_snapshot=ASSEMBLY_PARAMETER_BASIS,
        model_request=ASSEMBLY_PARAMETER_MODEL_REQUEST,
        model_response=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
        response_parse=ASSEMBLY_PARAMETER_RESPONSE_PARSE,
        recommendation=ASSEMBLY_PARAMETER_RECOMMENDATION,
        recommendation_manifest=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
        approval_decision=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ),
    "hic_scaffolding": ParameterRecommendationArtifactTypes(
        input_snapshot=HIC_PARAMETER_INPUT_SNAPSHOT,
        basis_snapshot=HIC_PARAMETER_BASIS,
        model_request=HIC_PARAMETER_MODEL_REQUEST,
        model_response=HIC_PARAMETER_MODEL_RESPONSE,
        response_parse=HIC_PARAMETER_RESPONSE_PARSE,
        recommendation=HIC_PARAMETER_RECOMMENDATION,
        recommendation_manifest=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
        approval_decision=HIC_PARAMETER_APPROVAL_DECISION,
    ),
    "t2t": ParameterRecommendationArtifactTypes(
        input_snapshot=T2T_PARAMETER_INPUT_SNAPSHOT,
        basis_snapshot=T2T_PARAMETER_BASIS,
        model_request=T2T_PARAMETER_POLICY_REQUEST,
        model_response=T2T_PARAMETER_POLICY_RESPONSE,
        response_parse=T2T_PARAMETER_RESPONSE_PARSE,
        recommendation=T2T_PARAMETER_RECOMMENDATION,
        recommendation_manifest=T2T_PARAMETER_RECOMMENDATION_MANIFEST,
        approval_decision=T2T_PARAMETER_APPROVAL_DECISION,
    ),
}


def load_parameter_recommendation_chain_v3(
    state: Mapping[str, Any],
    *,
    manifest_ref: ArtifactRef | Mapping[str, Any],
    expected_stage: Literal["assembly", "hic_scaffolding", "t2t"],
    expected_method_id: str,
) -> ParameterRecommendationChainV3:
    """Load one exact v3 chain; pre-v3 roots and mixed identities fail closed."""

    types = _PARAMETER_ARTIFACT_TYPES[expected_stage]
    workflow_step_id = _active_workflow_step_id(state, expected_stage=expected_stage)
    ref = _verified_current_occurrence_ref(
        state,
        manifest_ref,
        workflow_step_id=workflow_step_id,
        label="recommendation manifest",
    )
    manifest_record = read_parameter_json(
        state,
        ref,
        expected_type=types.recommendation_manifest,
    )
    _require_schema(manifest_record, 3, "recommendation manifest")
    try:
        manifest = ParameterRecommendationManifestV3.model_validate(manifest_record)
    except ValueError as exc:
        raise ParameterEvidenceError(
            "invalid schema-3 recommendation manifest"
        ) from exc
    if manifest.stage != expected_stage:
        raise ParameterEvidenceError(
            f"recommendation stage mismatch: expected {expected_stage!r}, "
            f"got {manifest.stage!r}"
        )
    if manifest.method_id != expected_method_id:
        raise ParameterEvidenceError(
            f"recommendation method mismatch: expected {expected_method_id!r}, "
            f"got {manifest.method_id!r}"
        )
    if manifest.previous_attempt_manifest_ref is not None:
        _verified_current_occurrence_ref(
            state,
            manifest.previous_attempt_manifest_ref,
            workflow_step_id=workflow_step_id,
            label="previous attempt manifest",
        )

    refs = manifest.refs
    input_snapshot_ref = _verified_current_occurrence_ref(
        state,
        refs.input_snapshot_ref,
        workflow_step_id=workflow_step_id,
        label="recommendation input snapshot",
    )
    input_record = read_parameter_json(
        state,
        input_snapshot_ref,
        expected_type=types.input_snapshot,
    )
    _require_schema(input_record, 3, "recommendation input snapshot")
    try:
        input_snapshot = RecommendationInputSnapshotV3.model_validate(input_record)
    except ValueError as exc:
        raise ParameterEvidenceError(
            "invalid schema-3 recommendation input snapshot"
        ) from exc

    basis_snapshot_ref = _verified_current_occurrence_ref(
        state,
        refs.basis_snapshot_ref,
        workflow_step_id=workflow_step_id,
        label="recommendation basis snapshot",
    )
    basis_snapshot = read_parameter_json(
        state,
        basis_snapshot_ref,
        expected_type=types.basis_snapshot,
    )
    _require_schema(basis_snapshot, 3, "recommendation basis snapshot")
    if basis_snapshot.get("method_id") != expected_method_id:
        raise ParameterEvidenceError("recommendation basis method identity mismatch")
    catalog_record = basis_snapshot.get("catalog_snapshot")
    if not isinstance(catalog_record, dict):
        raise ParameterEvidenceError(
            "recommendation basis has no catalog_snapshot mapping"
        )
    try:
        catalog = MethodParameterCatalog.model_validate(catalog_record)
    except ValueError as exc:
        raise ParameterEvidenceError(
            "recommendation catalog snapshot is invalid"
        ) from exc

    model_request_ref = _verified_current_occurrence_ref(
        state,
        refs.model_request_ref,
        workflow_step_id=workflow_step_id,
        label="parameter model request",
    )
    model_request = read_parameter_json(
        state,
        model_request_ref,
        expected_type=types.model_request,
    )
    _require_schema(model_request, 3, "parameter model request")
    if expected_stage == "assembly":
        try:
            validate_recorded_parameter_request(
                model_request,
                input_snapshot=input_snapshot,
                input_snapshot_sha256=input_snapshot_ref.sha256,
                basis_snapshot=basis_snapshot,
                basis_sha256=basis_snapshot_ref.sha256,
                catalog=catalog,
            )
        except ValueError as exc:
            raise ParameterEvidenceError(str(exc)) from exc
    model_response_ref = _verified_current_occurrence_ref(
        state,
        refs.model_response_ref,
        workflow_step_id=workflow_step_id,
        label="parameter model response",
    )
    model_response = read_parameter_json(
        state,
        model_response_ref,
        expected_type=types.model_response,
    )
    _require_schema(model_response, 2, "parameter model response")
    response_parse_ref = _verified_current_occurrence_ref(
        state,
        refs.response_parse_ref,
        workflow_step_id=workflow_step_id,
        label="parameter response parse",
    )
    response_parse = read_parameter_json(
        state,
        response_parse_ref,
        expected_type=types.response_parse,
    )
    _require_schema(response_parse, 1, "parameter response parse")
    recommendation_ref = _verified_current_occurrence_ref(
        state,
        refs.recommendation_ref,
        workflow_step_id=workflow_step_id,
        label="parameter recommendation",
    )
    recommendation = read_parameter_json(
        state,
        recommendation_ref,
        expected_type=types.recommendation,
    )
    _require_schema(recommendation, 2, "parameter recommendation")

    identities = {
        "input snapshot": (
            input_snapshot.method_id,
            input_snapshot.task,
        ),
        "catalog snapshot": (catalog.method_id, manifest.task),
        "recommendation": (
            recommendation.get("method_id"),
            recommendation.get("task"),
        ),
    }
    expected_identity = (expected_method_id, manifest.task)
    for label, identity in identities.items():
        if identity != expected_identity:
            raise ParameterEvidenceError(
                f"{label} identity does not match recommendation manifest"
            )
    if input_snapshot.stage_context.stage != expected_stage:
        raise ParameterEvidenceError(
            "recommendation input stage context does not match manifest"
        )

    return ParameterRecommendationChainV3(
        manifest_ref=ref,
        manifest=manifest,
        input_snapshot=input_snapshot,
        basis_snapshot=dict(basis_snapshot),
        catalog=catalog,
        model_request=dict(model_request),
        model_response=dict(model_response),
        response_parse=dict(response_parse),
        recommendation=dict(recommendation),
    )


def load_approved_parameter_chain_v3(
    state: Mapping[str, Any],
    *,
    attempt: ParameterAttempt | Mapping[str, Any],
    expected_stage: Literal["assembly", "hic_scaffolding", "t2t"],
    expected_method_id: str,
) -> ApprovedParameterBundleV3:
    """Load the immutable snapshot/catalog/L2 bundle approved for execution."""

    if not isinstance(attempt, ParameterAttempt):
        if attempt.get("schema_version") != 3:
            raise ParameterEvidenceError("unsupported_parameter_recommendation_schema")
        try:
            validated_attempt = ParameterAttempt.model_validate(attempt)
        except ValueError as exc:
            raise ParameterEvidenceError("invalid schema-3 parameter attempt") from exc
    else:
        validated_attempt = attempt
    if validated_attempt.schema_version != 3:
        raise ParameterEvidenceError("unsupported_parameter_recommendation_schema")
    if validated_attempt.method_id != expected_method_id:
        raise ParameterEvidenceError("approved parameter attempt method mismatch")
    workflow_step_id = _active_workflow_step_id(state, expected_stage=expected_stage)
    chain = load_parameter_recommendation_chain_v3(
        state,
        manifest_ref=validated_attempt.recommendation_manifest_ref,
        expected_stage=expected_stage,
        expected_method_id=expected_method_id,
    )
    manifest = chain.manifest
    if (
        validated_attempt.attempt_id != manifest.recommendation_id
        or validated_attempt.task != manifest.task
        or validated_attempt.previous_attempt_id != manifest.previous_attempt_id
        or validated_attempt.previous_attempt_seal_hash
        != manifest.previous_attempt_seal_hash
    ):
        raise ParameterEvidenceError(
            "approved parameter attempt does not match recommendation manifest"
        )

    types = _PARAMETER_ARTIFACT_TYPES[expected_stage]
    approval_decision_ref = _verified_current_occurrence_ref(
        state,
        validated_attempt.approval_decision_ref,
        workflow_step_id=workflow_step_id,
        label="parameter approval decision",
    )
    approval = read_parameter_json(
        state,
        approval_decision_ref,
        expected_type=types.approval_decision,
    )
    _require_schema(approval, 2, "parameter approval decision")
    if (
        approval.get("stage") != expected_stage
        or approval.get("gate_id") != "parameter_approval"
    ):
        raise ParameterEvidenceError("parameter approval decision identity mismatch")
    decision = approval.get("decision")
    if not isinstance(decision, dict):
        raise ParameterEvidenceError(
            "parameter approval decision has no decision mapping"
        )
    _require_schema(decision, 2, "parameter approval payload")
    if decision.get("decision") != "approve_parameters":
        raise ParameterEvidenceError("parameter recommendation was not approved")
    if decision.get("validated_recommendation_hash") != chain.manifest_ref.sha256:
        raise ParameterEvidenceError(
            "parameter approval is not bound to the recommendation manifest"
        )
    try:
        approved_manifest_ref = ArtifactRef.model_validate(
            decision.get("validated_recommendation_manifest_ref")
        )
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter approval has no validated manifest ref"
        ) from exc
    if approved_manifest_ref != chain.manifest_ref:
        raise ParameterEvidenceError(
            "parameter approval manifest ref does not match the attempt"
        )
    _verified_current_occurrence_ref(
        state,
        approved_manifest_ref,
        workflow_step_id=workflow_step_id,
        label="parameter approval manifest",
    )

    request_ref_value = approval.get("request_ref")
    try:
        request_ref = ArtifactRef.model_validate(request_ref_value)
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter approval decision has no review request ref"
        ) from exc
    if approval.get("request_hash") != request_ref.sha256:
        raise ParameterEvidenceError("parameter approval request hash mismatch")
    request_ref = _verified_current_occurrence_ref(
        state,
        request_ref,
        workflow_step_id=workflow_step_id,
        label="parameter approval request",
    )
    review_request = read_parameter_json(state, request_ref)
    _require_schema(review_request, 2, "parameter approval request")
    if (
        review_request.get("review_request_id") != approval.get("review_request_id")
        or review_request.get("stage") != expected_stage
        or review_request.get("gate_id") != "parameter_approval"
    ):
        raise ParameterEvidenceError("parameter approval request identity mismatch")
    payload = review_request.get("payload")
    if not isinstance(payload, dict):
        raise ParameterEvidenceError("parameter approval request has no payload")
    try:
        requested_manifest_ref = ArtifactRef.model_validate(
            payload.get("recommendation_manifest_ref")
        )
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter approval request has no recommendation manifest ref"
        ) from exc
    if (
        requested_manifest_ref != chain.manifest_ref
        or payload.get("recommendation_manifest_hash") != chain.manifest_ref.sha256
    ):
        raise ParameterEvidenceError(
            "parameter approval request does not match the recommendation manifest"
        )
    _verified_current_occurrence_ref(
        state,
        requested_manifest_ref,
        workflow_step_id=workflow_step_id,
        label="parameter approval request manifest",
    )

    # Force shape validation at the approved boundary, including unknown L2 keys
    # later rejected by the shared resolver.
    _ = ApprovedParameterBundleV3(
        attempt=validated_attempt,
        chain=chain,
        approval_decision=dict(approval),
    ).approved_l2
    return ApprovedParameterBundleV3(
        attempt=validated_attempt,
        chain=chain,
        approval_decision=dict(approval),
    )


def build_recommendation_dependency_refs(
    state: Mapping[str, Any],
    *,
    fact_source_paths: Mapping[
        Literal["read_qc_summary", "genome_survey_result"],
        ArtifactRef | Mapping[str, Any] | str | Path | None,
    ]
    | None = None,
    stage_input_paths: Mapping[
        str,
        ArtifactRef | Mapping[str, Any] | str | Path | None,
    ]
    | None = None,
    initial_input_snapshot_ref: ArtifactRef | Mapping[str, Any] | None = None,
    prior_attempt_manifest_refs: Sequence[ArtifactRef | Mapping[str, Any]] = (),
    motif_resolution_ref: ArtifactRef | Mapping[str, Any] | None = None,
) -> RecommendationDependencyRefs:
    """Build verified dependency refs in canonical role order."""

    fact_refs: dict[
        Literal["read_qc_summary", "genome_survey_result"],
        ArtifactRef,
    ] = {}
    for role, value in sorted((fact_source_paths or {}).items()):
        if value is not None:
            fact_refs[role] = _dependency_ref(state, value)

    stage_refs: list[ArtifactRef] = []
    seen: set[tuple[str, str, str]] = set()
    for _role, value in sorted((stage_input_paths or {}).items()):
        if value is None:
            continue
        ref = _dependency_ref(state, value)
        identity = (ref.artifact_type, ref.path_ref, ref.sha256)
        if identity in seen:
            raise ParameterEvidenceError(
                "stage input dependency paths must not resolve to duplicate refs"
            )
        seen.add(identity)
        stage_refs.append(ref)

    return RecommendationDependencyRefs(
        fact_source_refs=fact_refs,
        stage_input_refs=tuple(stage_refs),
        initial_input_snapshot_ref=(
            _dependency_ref(state, initial_input_snapshot_ref)
            if initial_input_snapshot_ref is not None
            else None
        ),
        prior_attempt_manifest_refs=tuple(
            _dependency_ref(state, ref) for ref in prior_attempt_manifest_refs
        ),
        motif_resolution_ref=(
            _dependency_ref(state, motif_resolution_ref)
            if motif_resolution_ref is not None
            else None
        ),
    )


def canonical_parameter_dependency(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any] | str | Path | None,
) -> ArtifactRef | Mapping[str, Any] | Path | None:
    """Keep only refs or paths eligible for the immutable dependency chain."""

    if value is None or isinstance(value, ArtifactRef):
        return value
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, (str, Path)):
        return None

    paths, _session_id = _session_paths(state)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = paths.run_dir / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(paths.artifacts_dir.resolve(strict=False))
    except ValueError:
        return None
    return candidate


def publish_parameter_json(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    payload: Mapping[str, Any],
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Publish one immutable, indexed parameter-evidence JSON object."""

    serialized = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    paths, _session_id = _session_paths(state)
    stage_component = _safe_component(stage, label="stage")
    workflow_step_id = current_workflow_step_id(
        cast(TopLevelState, state), expected_stage=stage_component
    )
    file_component = _safe_component(filename, label="filename")
    temporary = paths.workflow_step_work_path(
        workflow_step_id,
        stage_component,
        "parameter_persistence",
        f".{file_component}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(serialized)
    try:
        return publish_parameter_file(
            state,
            stage=stage,
            scope=scope,
            identifier=identifier,
            filename=filename,
            source=temporary,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    finally:
        temporary.unlink(missing_ok=True)


def publish_parameter_file(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    source: str | Path,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Move one work file into immutable, indexed parameter evidence.

    Only files below the current run's mutable ``work/`` tree may be consumed.
    Publishing is replay-safe: identical bytes reuse the existing ref, while a
    conflicting snapshot at the same logical path fails closed.
    """

    location = _parameter_evidence_location(
        state,
        stage=stage,
        scope=scope,
        identifier=identifier,
        filename=filename,
    )
    raw_source_path = Path(source).expanduser()
    if raw_source_path.is_symlink():
        raise ParameterEvidenceError(
            "parameter evidence source must not be a symbolic link"
        )
    source_path = raw_source_path.resolve(strict=False)
    try:
        source_path.relative_to(location.paths.work_dir.resolve(strict=False))
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter evidence source must be below the run work directory"
        ) from exc
    active_work_dir = location.paths.workflow_step_work_dir(
        location.workflow_step_id, location.stage
    ).resolve(strict=False)
    try:
        source_path.relative_to(active_work_dir)
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter evidence source targets another workflow occurrence"
        ) from exc
    artifact = ArtifactMeta.from_file(
        source_path,
        stage=location.stage,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={
            f"{scope}_id": identifier,
            "workflow_step_id": location.workflow_step_id,
        },
    )
    return publish_artifact_record(
        location.paths.run_dir,
        location.session_id,
        artifact,
        destination=location.destination,
    ).ref


def read_parameter_json(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Resolve and integrity-check one immutable parameter-evidence object."""

    try:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
    except ValueError as exc:
        raise ParameterEvidenceError("invalid parameter artifact reference") from exc
    if expected_type is not None and ref.artifact_type != expected_type:
        raise ParameterEvidenceError(
            f"parameter artifact type mismatch: expected {expected_type!r}, "
            f"got {ref.artifact_type!r}"
        )
    paths, session_id = _session_paths(state)
    try:
        path = resolve_artifact_ref(paths.run_dir, session_id, ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ParameterEvidenceError(
            f"parameter artifact is unreadable: {ref.path_ref}"
        ) from exc
    if not isinstance(payload, dict):
        raise ParameterEvidenceError(
            f"parameter artifact must contain a JSON object: {ref.path_ref}"
        )
    return payload


def recover_parameter_json(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    expected_type: str,
    method_name: str,
) -> tuple[ArtifactRef, dict[str, Any]] | None:
    """Recover deterministic parameter evidence after a stale checkpoint.

    A complete immutable file is sufficient to repair a crash between the
    atomic move and index append.  An existing row is integrity-checked.  A
    row whose file disappeared still fails closed because its response bytes
    cannot be reconstructed from metadata alone.
    """

    location = _parameter_evidence_location(
        state,
        stage=stage,
        scope=scope,
        identifier=identifier,
        filename=filename,
    )
    try:
        recovered = recover_published_artifact(
            location.paths.run_dir,
            location.session_id,
            location.destination,
            stage=location.stage,
            method_name=method_name,
            artifact_type=expected_type,
            metadata={
                f"{scope}_id": identifier,
                "workflow_step_id": location.workflow_step_id,
            },
        )
    except (OSError, ValueError) as exc:
        raise ParameterEvidenceError(
            "parameter artifact recovery failed: "
            f"{location.paths.to_ref(location.destination)}"
        ) from exc
    if recovered is None:
        return None
    ref = recovered.ref
    payload = read_parameter_json(state, ref, expected_type=expected_type)
    return ref, payload


def _parameter_evidence_location(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
) -> _ParameterEvidenceLocation:
    """Resolve the one logical destination shared by publish and recovery."""

    if scope not in {"recommendation", "attempt"}:
        raise ParameterEvidenceError(f"unsupported parameter evidence scope: {scope!r}")
    if type(identifier) is not int or identifier < 0:
        raise ParameterEvidenceError(f"{scope}_id must be a non-negative integer")
    stage_component = _safe_component(stage, label="stage")
    file_component = _safe_component(filename, label="filename")
    paths, session_id = _session_paths(state)
    workflow_step_id = current_workflow_step_id(
        cast(TopLevelState, state), expected_stage=stage_component
    )
    directory = "parameter_recommendations" if scope == "recommendation" else "attempts"
    destination = paths.workflow_step_artifact_path(
        workflow_step_id,
        stage_component,
        directory,
        f"{identifier:03d}",
        file_component,
    )
    return _ParameterEvidenceLocation(
        paths=paths,
        session_id=session_id,
        stage=stage_component,
        workflow_step_id=workflow_step_id,
        destination=destination,
    )


def _require_schema(record: Mapping[str, Any], expected: int, label: str) -> None:
    actual = record.get("schema_version")
    if type(actual) is not int or actual != expected:
        raise ParameterEvidenceError(
            "unsupported_parameter_recommendation_schema: "
            f"{label} requires schema {expected}, got {actual!r}"
        )


def _verified_ref(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
) -> ArtifactRef:
    try:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
        paths, session_id = _session_paths(state)
        resolve_artifact_ref(paths.run_dir, session_id, ref)
    except (OSError, ValueError) as exc:
        raise ParameterEvidenceError(
            "invalid or unreadable parameter artifact ref"
        ) from exc
    return ref


def _active_workflow_step_id(
    state: Mapping[str, Any],
    *,
    expected_stage: Literal["assembly", "hic_scaffolding", "t2t"],
) -> str:
    """Return the active occurrence ID required by parameter execution."""

    try:
        return current_workflow_step_id(
            cast(TopLevelState, state), expected_stage=expected_stage
        )
    except (TypeError, ValueError) as exc:
        raise ParameterEvidenceError(
            "parameter evidence requires the active workflow occurrence"
        ) from exc


def _verified_current_occurrence_ref(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    workflow_step_id: str,
    label: str,
) -> ArtifactRef:
    """Verify a ref and require its indexed artifact to belong to this occurrence."""

    ref = _verified_ref(state, value)
    paths, session_id = _session_paths(state)
    matches = [
        record
        for record in read_artifact_records(paths.run_dir, session_id)
        if record.get("artifact_id") == ref.artifact_id
        and record.get("path_ref") == ref.path_ref
        and record.get("sha256") == ref.sha256
        and record.get("artifact_type") == ref.artifact_type
    ]
    if len(matches) != 1:
        raise ParameterEvidenceError(f"{label} artifact identity is ambiguous")
    if matches[0].get("workflow_step_id") != workflow_step_id:
        raise ParameterEvidenceError(f"{label} targets another workflow occurrence")
    return ref


def _dependency_ref(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any] | str | Path,
) -> ArtifactRef:
    if isinstance(value, (ArtifactRef, Mapping)):
        return _verified_ref(state, value)
    try:
        paths, session_id = _session_paths(state)
        return artifact_ref_for_path(paths.run_dir, session_id, value)
    except (OSError, ValueError) as exc:
        raise ParameterEvidenceError(
            f"dependency path is not a verified run artifact: {value}"
        ) from exc


def _session_paths(state: Mapping[str, Any]) -> tuple[SessionPaths, str]:
    data_dir = state.get("data_dir")
    session_id = state.get("session_id")
    if not isinstance(data_dir, (str, Path)) or not isinstance(session_id, str):
        raise ParameterEvidenceError(
            "parameter evidence requires run data_dir and session_id"
        )
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    return paths, session_id


def _safe_component(value: str, *, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise ParameterEvidenceError(f"{label} must be one safe path component")
    return value


# ---------------------------------------------------------------------------
# Stage-bound persistence boundary
# ---------------------------------------------------------------------------


class ParameterPersistenceError(ValueError):
    """Raised at the workflow boundary when parameter evidence is inconsistent.

    Wraps :exc:`ParameterEvidenceError` so callers only need to catch one type.
    """


@dataclass(frozen=True)
class ParameterStageStorage:
    """Binds a stage name to the shared parameter-evidence persistence API.

    Instantiate once per stage module; expose ``write_artifact``,
    ``read_artifact``, ``recover_artifact``, and ``publish_file`` at module
    level.  ``ParameterPersistenceError`` is re-exported from this module so
    callers import both from the same place.
    """

    stage: str

    def write_artifact(
        self,
        state: Mapping[str, Any],
        identifier: int,
        filename: str,
        payload: Mapping[str, Any],
        *,
        artifact_type: str,
        method_name: str,
        scope: ParameterEvidenceScope = "recommendation",
    ) -> ArtifactRef:
        """Publish one immutable recommendation- or attempt-scoped JSON artifact."""

        try:
            return publish_parameter_json(
                state,
                stage=self.stage,
                scope=scope,
                identifier=identifier,
                filename=filename,
                payload=dict(payload),
                artifact_type=artifact_type,
                method_name=method_name,
            )
        except ParameterEvidenceError as exc:
            raise ParameterPersistenceError(str(exc)) from exc

    def read_artifact(
        self,
        state: Mapping[str, Any],
        value: ArtifactRef | Mapping[str, Any],
        *,
        expected_type: str | None = None,
    ) -> dict[str, Any]:
        """Read and integrity-check one immutable parameter artifact."""

        try:
            return read_parameter_json(state, value, expected_type=expected_type)
        except ParameterEvidenceError as exc:
            raise ParameterPersistenceError(str(exc)) from exc

    def recover_artifact(
        self,
        state: Mapping[str, Any],
        identifier: int,
        filename: str,
        *,
        expected_type: str,
        method_name: str,
        scope: ParameterEvidenceScope = "recommendation",
    ) -> tuple[ArtifactRef, dict[str, Any]] | None:
        """Recover an already-published parameter transform by logical path."""

        try:
            return recover_parameter_json(
                state,
                stage=self.stage,
                scope=scope,
                identifier=identifier,
                filename=filename,
                expected_type=expected_type,
                method_name=method_name,
            )
        except ParameterEvidenceError as exc:
            raise ParameterPersistenceError(str(exc)) from exc

    def publish_file(
        self,
        state: Mapping[str, Any],
        identifier: int,
        filename: str,
        source: str | Path,
        *,
        artifact_type: str,
        method_name: str,
        scope: ParameterEvidenceScope = "attempt",
    ) -> ArtifactRef:
        """Move one work file into immutable, indexed parameter evidence."""

        try:
            return publish_parameter_file(
                state,
                stage=self.stage,
                scope=scope,
                identifier=identifier,
                filename=filename,
                source=source,
                artifact_type=artifact_type,
                method_name=method_name,
            )
        except ParameterEvidenceError as exc:
            raise ParameterPersistenceError(str(exc)) from exc
