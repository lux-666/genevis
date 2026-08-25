"""Recoverable review intent and prepared-application records.

The immutable intent commits one validated operator/automatic decision.  The
application record proves that all gate-owned durable preparation completed;
only its typed effect may then be projected into a checkpoint update.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from app.control.workflow_plan import current_workflow_step_id
from app.integrity import SHA256_PATTERN
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.review.persistence import (
    ReviewPersistenceError,
    canonical_review_bytes,
    load_review_request,
    portable_review_value,
    publish_review_json_record,
)
from app.schemas.artifacts import ArtifactRef, canonical_artifact_refs
from app.schemas.state import DomainStage, TopLevelState
from app.storage.artifacts import read_artifact_records, resolve_artifact_ref
from app.storage.paths import DataLayoutError, SessionPaths

REVIEW_RECORD_MAX_BYTES = 128 * 1024
REVIEW_INTENT_ARTIFACT_TYPE = "review.intent.v1"
REVIEW_APPLICATION_ARTIFACT_TYPE = "review.application.v1"


class ReviewApplicationError(ValueError):
    """Base error for review transaction records."""


class ReviewIntentConflictError(ReviewApplicationError):
    """An immutable intent already commits a different decision."""


class ReviewApplicationConflictError(ReviewApplicationError):
    """An immutable application already commits a different effect."""


class ReviewApplicationIntegrityError(ReviewApplicationError):
    """A review intent/application or one of its refs is invalid."""


class RunRelativeFileRef(RootModel[str]):
    """Explicit, normalized reference to one existing file below the run root."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_path_ref(cls, value: str) -> str:
        if not value:
            raise ValueError("run-relative file ref must be non-empty")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("data/"):
            raise ValueError("run-relative file ref must not escape the run root")
        if path.as_posix() != value:
            raise ValueError("run-relative file ref must be normalized POSIX text")
        return value

    @property
    def path_ref(self) -> str:
        return self.root


class EmptyReviewMetadataV1(BaseModel):
    """Strict metadata contract for gates with no decision-owned metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ForegroundReviewMetadataV1(BaseModel):
    """Shared bounded countdown facts accepted by automatic review gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["foreground_countdown"] | None = None
    configured_seconds: int | None = Field(default=None, ge=0)
    elapsed_seconds: int | float | None = Field(default=None, ge=0)

    @field_validator("configured_seconds", "elapsed_seconds", mode="before")
    @classmethod
    def reject_boolean_seconds(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("foreground countdown values must be numeric")
        return value

    @model_validator(mode="after")
    def validate_countdown(self) -> "ForegroundReviewMetadataV1":
        values = (self.source, self.configured_seconds, self.elapsed_seconds)
        if all(value is None for value in values):
            return self
        if any(value is None for value in values):
            raise ValueError("foreground countdown metadata must be complete")
        assert self.configured_seconds is not None
        assert self.elapsed_seconds is not None
        if self.elapsed_seconds < self.configured_seconds:
            raise ValueError("foreground countdown elapsed_seconds is too small")
        return self


class ReviewApplicationIdentity(BaseModel):
    """Full identity of one review request occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    workflow_step_id: str = Field(min_length=1)
    stage: DomainStage
    gate_id: str = Field(min_length=1)
    review_request_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_PATTERN)

    @classmethod
    def from_intent(cls, intent: "ReviewIntentV1") -> "ReviewApplicationIdentity":
        return cls(
            session_id=intent.session_id,
            workflow_step_id=intent.workflow_step_id,
            stage=intent.stage,
            gate_id=intent.gate_id,
            review_request_id=intent.review_request_id,
            request_hash=intent.request_hash,
        )


class ReviewIntentV1(BaseModel):
    """Canonical first persistence boundary for one submitted decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    workflow_step_id: str = Field(min_length=1)
    stage: DomainStage
    gate_id: str = Field(min_length=1)
    review_request_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    request_ref: ArtifactRef
    source: str = Field(min_length=1, max_length=128)
    blocked_timeout_seconds: float | None = Field(default=None, ge=0)
    request: ResumeReviewRequest

    @model_validator(mode="after")
    def validate_identity(self) -> "ReviewIntentV1":
        expected = {
            "session_id": self.session_id,
            "stage": self.stage,
            "gate_id": self.gate_id,
            "review_request_id": self.review_request_id,
            "review_request_hash": self.request_hash,
        }
        for field, value in expected.items():
            if getattr(self.request, field) != value:
                raise ValueError(f"intent request {field} does not match its envelope")
        if self.request_ref.sha256 != self.request_hash:
            raise ValueError("intent request_hash does not match request_ref.sha256")
        return self


class ReviewApplicationV1(BaseModel):
    """Immutable commit marker for completed gate-specific preparation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    application_id: str = Field(pattern=r"^review_app_[0-9a-f]{24}$")
    session_id: str = Field(min_length=1)
    workflow_step_id: str = Field(min_length=1)
    stage: DomainStage
    gate_id: str = Field(min_length=1)
    review_request_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    intent_ref: ArtifactRef
    decision_ref: ArtifactRef
    effect_kind: str = Field(min_length=1, max_length=160)
    effect: dict[str, Any]
    required_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def validate_application_id(self) -> "ReviewApplicationV1":
        if self.required_refs != canonical_artifact_refs(self.required_refs):
            raise ValueError("application required_refs must be sorted and unique")
        payload = self.model_dump(mode="json", exclude={"application_id"})
        if self.application_id != review_application_id(payload):
            raise ValueError("application_id does not match canonical application")
        return self


@dataclass(frozen=True)
class PersistedReviewIntent:
    """Verified intent model and its exact indexed artifact ref."""

    intent: ReviewIntentV1
    ref: ArtifactRef


@dataclass(frozen=True)
class PreparedReviewApplication:
    """Gate-owned durable preparation ready for immutable publication."""

    effect_kind: str
    effect: BaseModel
    decision_ref: ArtifactRef
    required_refs: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class VerifiedReviewApplication:
    """Application envelope plus its registry-decoded typed effect."""

    application: ReviewApplicationV1
    ref: ArtifactRef
    effect: BaseModel


def review_application_id(payload_without_application_id: Mapping[str, Any]) -> str:
    """Return the deterministic ID whose hash preimage excludes the ID itself."""

    payload = dict(payload_without_application_id)
    payload.pop("application_id", None)
    from hashlib import sha256

    return f"review_app_{sha256(canonical_review_bytes(payload)).hexdigest()[:24]}"


def portable_resume_review_request(
    data_dir: str | Path,
    request: ResumeReviewRequest,
) -> ResumeReviewRequest:
    """Return the canonical run-portable form used inside ``intent.json``."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(request.session_id)
    try:
        portable = _strict_portable_review_value(
            paths,
            request.model_dump(mode="json"),
        )
        return ResumeReviewRequest.model_validate(portable)
    except (DataLayoutError, TypeError, ValueError) as exc:
        raise ReviewApplicationIntegrityError(
            f"resume request is not run-portable: {exc}"
        ) from exc


def canonicalize_review_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    request_record: Mapping[str, Any] | None = None,
) -> ResumeReviewRequest:
    """Apply the sole gate-specific, write-free durable intent contract."""

    paths, identity, request_ref = _identity_from_blocked_state(state, request)
    record = request_record
    if record is None:
        try:
            record = load_review_request(
                paths.run_dir,
                request_ref,
                session_id=identity.session_id,
            )
        except ReviewPersistenceError as exc:
            raise ReviewApplicationIntegrityError(str(exc)) from exc
    _require_request_record_identity(record, identity)

    gate = get_review_gate(identity.stage, identity.gate_id)
    portable = portable_resume_review_request(paths.run_dir, request)
    canonical = _canonical_request_metadata(paths, gate, portable)
    validator = gate.load_validate_intent_request()
    try:
        validated = validator(state, canonical, record)
    except ReviewApplicationError:
        raise
    except Exception:
        raise
    if not isinstance(validated, ResumeReviewRequest):
        raise ReviewApplicationIntegrityError(
            "review gate intent validator must return ResumeReviewRequest"
        )
    validated = portable_resume_review_request(paths.run_dir, validated)
    validated = _canonical_request_metadata(paths, gate, validated)
    _identity_from_blocked_state(state, validated)
    _verify_typed_references(paths, validated, "review intent request")
    _require_bounded_record(validated.model_dump(mode="json"), "resume request")
    return validated


def review_identity_from_blocked_state(
    state: TopLevelState,
) -> ReviewApplicationIdentity:
    """Verify and return the exact active blocked review occurrence identity."""

    paths = _paths_for_state(state)
    if state.get("supervision_state") != "blocked_human":
        raise ReviewApplicationIntegrityError(
            "review occurrence identity requires blocked_human"
        )
    interrupt = state.get("active_interrupt")
    if not isinstance(interrupt, Mapping):
        raise ReviewApplicationIntegrityError("blocked review has no active_interrupt")
    stage = interrupt.get("stage")
    gate_id = interrupt.get("gate_id")
    request_id = interrupt.get("review_request_id")
    request_hash = interrupt.get("request_hash")
    if not isinstance(stage, str) or not stage:
        raise ReviewApplicationIntegrityError(
            "active review stage is missing or invalid"
        )
    if not isinstance(gate_id, str) or not gate_id:
        raise ReviewApplicationIntegrityError(
            "active review gate_id is missing or invalid"
        )
    if not isinstance(request_id, str) or not request_id:
        raise ReviewApplicationIntegrityError(
            "active review request ID is missing or invalid"
        )
    if not isinstance(request_hash, str) or not request_hash:
        raise ReviewApplicationIntegrityError(
            "active review request hash is missing or invalid"
        )
    try:
        workflow_step_id = current_workflow_step_id(
            state,
            expected_stage=str(stage),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewApplicationIntegrityError(str(exc)) from exc
    if interrupt.get("workflow_step_id") != workflow_step_id:
        raise ReviewApplicationIntegrityError(
            "active_interrupt workflow_step_id does not match checkpoint"
        )
    request_ref = _coerce_ref(interrupt.get("request_ref"), "active request_ref")
    if request_ref.sha256 != request_hash:
        raise ReviewApplicationIntegrityError(
            "active request hash does not match request_ref"
        )
    identity = ReviewApplicationIdentity(
        session_id=state["session_id"],
        workflow_step_id=workflow_step_id,
        stage=stage,
        gate_id=gate_id,
        review_request_id=request_id,
        request_hash=request_hash,
    )
    _resolve_ref(paths, request_ref, "review request")
    request_record = load_review_request(
        paths.run_dir,
        request_ref,
        session_id=identity.session_id,
    )
    _require_request_record_identity(request_record, identity)
    return identity


def persist_review_intent(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    source: str,
    blocked_timeout_seconds: float | None = None,
) -> PersistedReviewIntent:
    """Publish or recover the sole canonical decision intent for a request."""

    portable_request = canonicalize_review_intent_request(state, request)
    paths, identity, request_ref = _identity_from_blocked_state(
        state,
        portable_request,
    )
    intent = ReviewIntentV1(
        **identity.model_dump(mode="python"),
        request_ref=request_ref,
        source=source,
        blocked_timeout_seconds=blocked_timeout_seconds,
        request=portable_request,
    )
    _require_bounded_record(intent.model_dump(mode="json"), "review intent")
    destination = paths.workflow_step_artifact_path(
        identity.workflow_step_id,
        identity.stage,
        "reviews",
        identity.review_request_id,
        "intent.json",
    )
    try:
        ref = publish_review_json_record(
            paths,
            session_id=identity.session_id,
            destination=destination,
            payload=intent.model_dump(mode="json"),
            stage=identity.stage,
            method_name=f"{identity.stage}.review.{identity.gate_id}.intent",
            artifact_type=REVIEW_INTENT_ARTIFACT_TYPE,
            metadata={
                "workflow_step_id": identity.workflow_step_id,
                "review_request_id": identity.review_request_id,
            },
        )
    except FileExistsError as exc:
        raise ReviewIntentConflictError(
            f"review intent conflicts with immutable intent.json: {destination}"
        ) from exc
    return PersistedReviewIntent(
        intent=load_verified_review_intent(
            paths.run_dir,
            ref,
            expected_identity=identity,
        ),
        ref=ref,
    )


def persist_review_application(
    state: TopLevelState,
    *,
    intent: PersistedReviewIntent,
    prepared: PreparedReviewApplication,
) -> ArtifactRef:
    """Publish or recover the immutable marker for completed preparation."""

    paths = _paths_for_state(state)
    identity = ReviewApplicationIdentity.from_intent(intent.intent)
    _require_state_identity(state, identity)
    verified_intent = load_verified_review_intent(
        paths.run_dir,
        intent.ref,
        expected_identity=identity,
    )
    if verified_intent != intent.intent:
        raise ReviewApplicationIntegrityError(
            "persisted intent model does not match ref"
        )

    gate = get_review_gate(identity.stage, identity.gate_id)
    if getattr(gate, "effect_kind", None) != prepared.effect_kind:
        raise ReviewApplicationIntegrityError(
            "prepared effect_kind does not match the registered review gate"
        )
    effect_payload = portable_review_value(
        paths,
        prepared.effect.model_dump(mode="json"),
    )
    try:
        decoded_effect = gate.decode_effect(effect_payload)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"prepared review effect failed registered decoding: {exc}"
        ) from exc
    if not isinstance(decoded_effect, BaseModel):
        raise ReviewApplicationIntegrityError(
            "registered review effect decoder must return a Pydantic model"
        )
    effect_payload = decoded_effect.model_dump(mode="json")
    _verify_decision_ref(paths, prepared.decision_ref, intent.intent)
    required_refs = canonical_artifact_refs(
        (
            *prepared.required_refs,
            *_typed_artifact_refs(decoded_effect),
            *_dynamic_metadata_artifact_refs(
                decoded_effect,
                "prepared review effect",
            ),
        )
    )
    _verify_typed_references(paths, decoded_effect, "prepared review effect")
    for required_ref in required_refs:
        _resolve_ref(paths, required_ref, "required application ref")
    _require_effect_bindings(
        decoded_effect,
        identity=identity,
        intent=intent.intent,
        decision_ref=prepared.decision_ref,
    )

    payload: dict[str, Any] = {
        "schema_version": 1,
        **identity.model_dump(mode="json"),
        "intent_ref": intent.ref.model_dump(mode="json"),
        "decision_ref": prepared.decision_ref.model_dump(mode="json"),
        "effect_kind": prepared.effect_kind,
        "effect": effect_payload,
        "required_refs": [ref.model_dump(mode="json") for ref in required_refs],
    }
    application = ReviewApplicationV1(
        **payload,
        application_id=review_application_id(payload),
    )
    _require_bounded_record(application.model_dump(mode="json"), "review application")
    destination = paths.workflow_step_artifact_path(
        identity.workflow_step_id,
        identity.stage,
        "reviews",
        identity.review_request_id,
        "application.json",
    )
    try:
        ref = publish_review_json_record(
            paths,
            session_id=identity.session_id,
            destination=destination,
            payload=application.model_dump(mode="json"),
            stage=identity.stage,
            method_name=f"{identity.stage}.review.{identity.gate_id}.application",
            artifact_type=REVIEW_APPLICATION_ARTIFACT_TYPE,
            metadata={
                "workflow_step_id": identity.workflow_step_id,
                "review_request_id": identity.review_request_id,
            },
        )
    except FileExistsError as exc:
        raise ReviewApplicationConflictError(
            "review application conflicts with immutable application.json: "
            f"{destination}"
        ) from exc
    load_verified_review_application(
        paths.run_dir,
        ref,
        expected_identity=identity,
    )
    return ref


def load_verified_review_intent(
    data_dir: str | Path,
    intent_ref: ArtifactRef | Mapping[str, Any],
    *,
    expected_identity: ReviewApplicationIdentity | None = None,
) -> ReviewIntentV1:
    """Strictly verify index, bytes, schema, request binding, and occurrence."""

    paths = SessionPaths.from_run_dir(data_dir)
    ref = _coerce_ref(intent_ref, "intent_ref")
    if ref.artifact_type != REVIEW_INTENT_ARTIFACT_TYPE:
        raise ReviewApplicationIntegrityError(
            f"intent_ref has unexpected artifact_type: {ref.artifact_type}"
        )
    path = _resolve_ref(paths, ref, "review intent")
    payload = _read_json_record(path, ref, "review intent")
    try:
        intent = ReviewIntentV1.model_validate(payload)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"invalid review intent record {ref.path_ref}: {exc}"
        ) from exc
    identity = ReviewApplicationIdentity.from_intent(intent)
    if expected_identity is not None:
        _require_matching_identity(identity, expected_identity)
    _require_index_identity(paths, ref, identity)
    _require_expected_record_path(paths, ref, identity, "intent.json")
    _resolve_ref(paths, intent.request_ref, "review request")
    try:
        request_record = load_review_request(
            paths.run_dir,
            intent.request_ref,
            session_id=intent.session_id,
        )
    except ReviewPersistenceError as exc:
        raise ReviewApplicationIntegrityError(str(exc)) from exc
    _require_request_record_identity(request_record, identity)
    portable_request = portable_resume_review_request(paths.run_dir, intent.request)
    gate = get_review_gate(identity.stage, identity.gate_id)
    canonical_request = _canonical_request_metadata(paths, gate, portable_request)
    if canonical_request != intent.request:
        raise ReviewApplicationIntegrityError(
            "review intent request is not in canonical gate wire form"
        )
    _verify_typed_references(paths, canonical_request, "review intent request")
    return intent


def load_verified_review_application(
    data_dir: str | Path,
    application_ref: ArtifactRef | Mapping[str, Any],
    *,
    expected_identity: ReviewApplicationIdentity | None = None,
) -> VerifiedReviewApplication:
    """Load the exact registry-typed application trusted by checkpoint apply."""

    paths = SessionPaths.from_run_dir(data_dir)
    if expected_identity is not None:
        paths.require_session_id(expected_identity.session_id)
    ref = _coerce_ref(application_ref, "application_ref")
    if ref.artifact_type != REVIEW_APPLICATION_ARTIFACT_TYPE:
        raise ReviewApplicationIntegrityError(
            f"application_ref has unexpected artifact_type: {ref.artifact_type}"
        )
    path = _resolve_ref(paths, ref, "review application")
    payload = _read_json_record(path, ref, "review application")
    try:
        application = ReviewApplicationV1.model_validate(payload)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"invalid review application record {ref.path_ref}: {exc}"
        ) from exc
    actual_identity = ReviewApplicationIdentity(
        session_id=application.session_id,
        workflow_step_id=application.workflow_step_id,
        stage=application.stage,
        gate_id=application.gate_id,
        review_request_id=application.review_request_id,
        request_hash=application.request_hash,
    )
    paths.require_session_id(actual_identity.session_id)
    if expected_identity is not None:
        _require_matching_identity(actual_identity, expected_identity)
    _require_index_identity(paths, ref, actual_identity)
    _require_expected_record_path(paths, ref, actual_identity, "application.json")
    intent = load_verified_review_intent(
        paths.run_dir,
        application.intent_ref,
        expected_identity=actual_identity,
    )
    _verify_decision_ref(paths, application.decision_ref, intent)
    for required_ref in application.required_refs:
        _resolve_ref(paths, required_ref, "required application ref")

    gate = get_review_gate(actual_identity.stage, actual_identity.gate_id)
    registered_kind = getattr(gate, "effect_kind", None)
    if application.effect_kind != registered_kind:
        raise ReviewApplicationIntegrityError(
            "application effect_kind does not match the registered review gate"
        )
    try:
        effect = gate.decode_effect(application.effect)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"application effect failed registered decoding: {exc}"
        ) from exc
    if not isinstance(effect, BaseModel):
        raise ReviewApplicationIntegrityError(
            "registered review effect decoder must return a Pydantic model"
        )
    _verify_typed_references(paths, effect, "review application effect")
    typed_refs = canonical_artifact_refs(_typed_artifact_refs(effect))
    dynamic_metadata_refs = canonical_artifact_refs(
        _dynamic_metadata_artifact_refs(effect, "review application effect")
    )
    missing_typed_refs = [
        ref for ref in typed_refs if ref not in application.required_refs
    ]
    if missing_typed_refs:
        raise ReviewApplicationIntegrityError(
            "application required_refs omit typed effect refs: "
            + ", ".join(ref.path_ref for ref in missing_typed_refs)
        )
    missing_dynamic_metadata_refs = [
        ref for ref in dynamic_metadata_refs if ref not in application.required_refs
    ]
    if missing_dynamic_metadata_refs:
        raise ReviewApplicationIntegrityError(
            "application required_refs omit dynamic metadata refs: "
            + ", ".join(ref.path_ref for ref in missing_dynamic_metadata_refs)
        )
    _require_effect_bindings(
        effect,
        identity=actual_identity,
        intent=intent,
        decision_ref=application.decision_ref,
    )
    return VerifiedReviewApplication(
        application=application,
        ref=ref,
        effect=effect,
    )


def _canonical_request_metadata(
    paths: SessionPaths,
    gate: Any,
    request: ResumeReviewRequest,
) -> ResumeReviewRequest:
    try:
        metadata = gate.decode_intent_metadata(request.metadata)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"resume request metadata failed the registered gate schema: {exc}"
        ) from exc
    if not isinstance(metadata, BaseModel):
        raise ReviewApplicationIntegrityError(
            "registered intent metadata decoder must return a Pydantic model"
        )
    _verify_typed_references(paths, metadata, "review intent metadata")
    canonical_metadata = metadata.model_dump(mode="json", exclude_unset=True)
    return request.model_copy(update={"metadata": canonical_metadata})


def _strict_portable_review_value(paths: SessionPaths, value: Any) -> Any:
    """Canonicalize run-local absolute paths and reject external absolute paths."""

    if hasattr(value, "model_dump"):
        return _strict_portable_review_value(paths, value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _strict_portable_review_value(paths, item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_strict_portable_review_value(paths, item) for item in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return paths.to_ref(candidate)
    return value


def _typed_reference_values(value: Any) -> tuple[ArtifactRef | RunRelativeFileRef, ...]:
    refs: list[ArtifactRef | RunRelativeFileRef] = []

    def visit(item: Any) -> None:
        if isinstance(item, ArtifactRef):
            refs.append(item)
            return
        if isinstance(item, RunRelativeFileRef):
            refs.append(item)
            return
        if isinstance(item, BaseModel):
            for field_name in type(item).model_fields:
                visit(getattr(item, field_name))
            return
        if isinstance(item, (tuple, list)):
            for child in item:
                visit(child)
            return
        # Arbitrary mappings are intentionally opaque. Ref-bearing metadata must
        # be represented by a registered typed model or listed in required_refs.

    visit(value)
    return tuple(refs)


def _typed_artifact_refs(value: Any) -> tuple[ArtifactRef, ...]:
    return tuple(
        ref for ref in _typed_reference_values(value) if isinstance(ref, ArtifactRef)
    )


def _dynamic_metadata_artifact_refs(value: Any, label: str) -> tuple[ArtifactRef, ...]:
    """Validate opaque effect mappings and return their serialized artifact refs.

    Typed references are collected separately by :func:`_typed_artifact_refs`.
    Arbitrary JSON metadata may still carry durable artifact identities, but
    those must be recorded explicitly in ``required_refs``.  This walker keeps
    ordinary typed string fields (notably inventory resource paths) untouched
    while rejecting non-portable paths only after entering an opaque mapping.
    """

    refs: list[ArtifactRef] = []
    artifact_ref_markers = frozenset(ArtifactRef.model_fields) - {"schema_version"}

    def visit(item: Any, *, in_opaque_mapping: bool = False) -> None:
        if isinstance(item, (ArtifactRef, RunRelativeFileRef)):
            return
        if isinstance(item, BaseModel):
            for field_name in type(item).model_fields:
                visit(getattr(item, field_name))
            return
        if isinstance(item, Mapping):
            marker_count = len(artifact_ref_markers.intersection(item))
            if "artifact_id" in item or marker_count >= 2:
                try:
                    refs.append(ArtifactRef.model_validate(item))
                except Exception as exc:
                    raise ReviewApplicationIntegrityError(
                        f"{label} has an invalid serialized ArtifactRef"
                    ) from exc
                return
            for child in item.values():
                visit(child, in_opaque_mapping=True)
            return
        if isinstance(item, (tuple, list)):
            for child in item:
                visit(child, in_opaque_mapping=in_opaque_mapping)
            return
        if in_opaque_mapping and isinstance(item, (str, Path)):
            candidate = Path(item).expanduser()
            if candidate.is_absolute():
                raise ReviewApplicationIntegrityError(
                    f"{label} has an external absolute path in dynamic metadata"
                )

    visit(value)
    return tuple(refs)


def _verify_typed_references(
    paths: SessionPaths,
    value: Any,
    label: str,
) -> None:
    for ref in _typed_reference_values(value):
        if isinstance(ref, ArtifactRef):
            _resolve_ref(paths, ref, label)
            continue
        try:
            path = paths.resolve_ref(ref.path_ref)
        except DataLayoutError as exc:
            raise ReviewApplicationIntegrityError(
                f"invalid {label} run-relative ref: {exc}"
            ) from exc
        if not path.is_file():
            raise ReviewApplicationIntegrityError(
                f"{label} run-relative ref is missing: {ref.path_ref}"
            )


def _require_effect_bindings(
    effect: BaseModel,
    *,
    identity: ReviewApplicationIdentity,
    intent: ReviewIntentV1,
    decision_ref: ArtifactRef,
) -> None:
    fields = type(effect).model_fields
    expected = {
        "decision_ref": decision_ref,
        "review_request_id": identity.review_request_id,
        "review_request_hash": identity.request_hash,
        "review_request_ref": intent.request_ref,
    }
    for field_name, expected_value in expected.items():
        if field_name not in fields:
            continue
        if getattr(effect, field_name) != expected_value:
            raise ReviewApplicationIntegrityError(
                f"review effect {field_name} does not match its application envelope"
            )


def _identity_from_blocked_state(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[SessionPaths, ReviewApplicationIdentity, ArtifactRef]:
    paths = _paths_for_state(state)
    identity = review_identity_from_blocked_state(state)
    if request.session_id != state["session_id"]:
        raise ReviewApplicationIntegrityError(
            "resume request session_id does not match"
        )
    interrupt = state.get("active_interrupt")
    assert isinstance(interrupt, Mapping)
    request_identity = {
        "stage": request.stage,
        "gate_id": request.gate_id,
        "review_request_id": request.review_request_id,
        "request_hash": request.review_request_hash,
    }
    for field, actual in request_identity.items():
        if actual != getattr(identity, field):
            raise ReviewApplicationIntegrityError(
                f"resume request {field} does not match active review occurrence"
            )
    request_ref = _coerce_ref(interrupt.get("request_ref"), "active request_ref")
    return paths, identity, request_ref


def _paths_for_state(state: TopLevelState) -> SessionPaths:
    paths = SessionPaths.from_run_dir(state.get("data_dir", ""))
    paths.require_session_id(state["session_id"])
    return paths


def _require_state_identity(
    state: TopLevelState,
    identity: ReviewApplicationIdentity,
) -> None:
    paths = _paths_for_state(state)
    if paths.session_id != identity.session_id:
        raise ReviewApplicationIntegrityError("state session does not match intent")
    try:
        step_id = current_workflow_step_id(state, expected_stage=identity.stage)
    except (TypeError, ValueError) as exc:
        raise ReviewApplicationIntegrityError(str(exc)) from exc
    if step_id != identity.workflow_step_id:
        raise ReviewApplicationIntegrityError(
            "state workflow_step_id does not match review intent"
        )


def _coerce_ref(value: Any, name: str) -> ArtifactRef:
    try:
        return (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"{name} is not a valid ArtifactRef"
        ) from exc


def _resolve_ref(paths: SessionPaths, ref: ArtifactRef, label: str) -> Path:
    try:
        return resolve_artifact_ref(paths.run_dir, paths.session_id, ref)
    except (DataLayoutError, FileNotFoundError, ValueError) as exc:
        raise ReviewApplicationIntegrityError(f"invalid {label} ref: {exc}") from exc


def _read_json_record(path: Path, ref: ArtifactRef, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > REVIEW_RECORD_MAX_BYTES:
            raise ReviewApplicationIntegrityError(
                f"{label} exceeds {REVIEW_RECORD_MAX_BYTES} bytes: {ref.path_ref}"
            )
        payload = json.loads(raw)
    except ReviewApplicationIntegrityError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewApplicationIntegrityError(
            f"{label} is unreadable: {ref.path_ref}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewApplicationIntegrityError(
            f"{label} must be a JSON object: {ref.path_ref}"
        )
    return payload


def _require_bounded_record(payload: Mapping[str, Any], label: str) -> None:
    try:
        size = len(canonical_review_bytes(payload))
    except (TypeError, ValueError) as exc:
        raise ReviewApplicationIntegrityError(f"{label} is not canonical JSON") from exc
    if size > REVIEW_RECORD_MAX_BYTES:
        raise ReviewApplicationIntegrityError(
            f"{label} exceeds {REVIEW_RECORD_MAX_BYTES} bytes"
        )


def _require_matching_identity(
    actual: ReviewApplicationIdentity,
    expected: ReviewApplicationIdentity,
) -> None:
    for field in ReviewApplicationIdentity.model_fields:
        if getattr(actual, field) != getattr(expected, field):
            raise ReviewApplicationIntegrityError(
                f"review application {field} does not match expected occurrence"
            )


def _require_request_record_identity(
    record: Mapping[str, Any],
    identity: ReviewApplicationIdentity,
) -> None:
    expected = {
        "workflow_step_id": identity.workflow_step_id,
        "stage": identity.stage,
        "gate_id": identity.gate_id,
        "review_request_id": identity.review_request_id,
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise ReviewApplicationIntegrityError(
                f"review request {field} does not match expected occurrence"
            )


def _require_index_identity(
    paths: SessionPaths,
    ref: ArtifactRef,
    identity: ReviewApplicationIdentity,
) -> None:
    record = next(
        (
            item
            for item in read_artifact_records(paths.run_dir, paths.session_id)
            if item.get("artifact_id") == ref.artifact_id
        ),
        None,
    )
    if record is None:
        raise ReviewApplicationIntegrityError(
            f"artifact is not indexed: {ref.artifact_id}"
        )
    expected = {
        "workflow_step_id": identity.workflow_step_id,
        "stage": identity.stage,
        "review_request_id": identity.review_request_id,
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise ReviewApplicationIntegrityError(
                f"artifact index {field} does not match review occurrence"
            )


def _require_expected_record_path(
    paths: SessionPaths,
    ref: ArtifactRef,
    identity: ReviewApplicationIdentity,
    filename: str,
) -> None:
    expected = paths.to_ref(
        paths.workflow_step_artifact_path(
            identity.workflow_step_id,
            identity.stage,
            "reviews",
            identity.review_request_id,
            filename,
        )
    )
    if ref.path_ref != expected:
        raise ReviewApplicationIntegrityError(
            f"review {filename} ref does not use its canonical occurrence path"
        )


def _verify_decision_ref(
    paths: SessionPaths,
    decision_ref: ArtifactRef,
    intent: ReviewIntentV1,
) -> None:
    path = _resolve_ref(paths, decision_ref, "review decision")
    payload = _read_json_record(path, decision_ref, "review decision")
    expected = {
        "schema_version": 2,
        "workflow_step_id": intent.workflow_step_id,
        "review_request_id": intent.review_request_id,
        "stage": intent.stage,
        "gate_id": intent.gate_id,
        "request_hash": intent.request_hash,
        "request_ref": intent.request_ref.model_dump(mode="json"),
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ReviewApplicationIntegrityError(
                f"review decision {field} does not match committed intent"
            )
    identity = ReviewApplicationIdentity.from_intent(intent)
    _require_index_identity(paths, decision_ref, identity)
    _require_expected_record_path(paths, decision_ref, identity, "decision.json")
