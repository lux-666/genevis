"""Immutable, stage-neutral persistence for human-review requests and decisions.

Review policy and decision application stay in their existing domain modules.
This module owns only the portable evidence records and the compact checkpoint
cursor shared by every gate, including parameter approval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from app.execution.artifacts import append_artifact_records
from app.schemas.artifacts import ArtifactMeta, ArtifactRef, file_sha256
from app.schemas.state import TopLevelState
from app.storage.paths import DataLayoutError, SessionPaths
from app.storage.publish import publish_file


class ReviewPersistenceError(ValueError):
    """Raised when immutable review evidence is invalid or corrupt."""


@dataclass(frozen=True)
class PersistedReviewRequest:
    """Canonical identity and ref returned after publishing a request."""

    review_request_id: str
    request_ref: ArtifactRef

    @property
    def request_hash(self) -> str:
        return self.request_ref.sha256

    def compact_interrupt(
        self,
        *,
        stage: str,
        gate_id: str,
        reason: str,
        blocked_at: str | None = None,
        bindings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the bounded checkpoint cursor; request content stays on disk."""

        interrupt: dict[str, Any] = {
            "stage": stage,
            "gate_id": gate_id,
            "reason": reason,
            "review_request_id": self.review_request_id,
            "request_ref": self.request_ref.model_dump(mode="json"),
            "request_hash": self.request_hash,
            "blocked_at": blocked_at or datetime.now(timezone.utc).isoformat(),
        }
        if bindings:
            allowed = {
                "attempt_id",
                "previous_attempt_id",
                "task",
                "recommendation_hash",
            }
            unknown = set(bindings) - allowed
            if unknown:
                raise ReviewPersistenceError(
                    "unsupported compact interrupt bindings: " + ", ".join(sorted(unknown))
                )
            interrupt.update(bindings)
        return interrupt


def persist_review_request(
    state: TopLevelState,
    *,
    stage: str,
    gate_id: str,
    reason: str,
    payload: Mapping[str, Any] | Any,
    method_name: str | None = None,
    artifact_type: str | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> PersistedReviewRequest:
    """Publish one content-addressed review request below ``artifacts/reviews``.

    The ID is stable for the same stage, gate, bindings, and portable payload,
    so a replay reuses the same immutable file and artifact-index row.
    """

    paths = _paths_for_state(state)
    request_body = _review_request_body(
        _dump_payload(payload),
        stage=stage,
        gate_id=gate_id,
        reason=reason,
    )
    portable_payload = _portable_value(paths, request_body)
    portable_bindings = _portable_value(paths, dict(bindings or {}))
    identity_payload = {
        "schema_version": 2,
        "stage": stage,
        "gate_id": gate_id,
        "reason": reason,
        "bindings": portable_bindings,
        "payload": portable_payload,
    }
    identity_bytes = _canonical_bytes(identity_payload)
    review_request_id = f"review_{sha256(identity_bytes).hexdigest()[:24]}"
    record = {
        **identity_payload,
        "review_request_id": review_request_id,
    }
    destination = paths.artifact_path("reviews", review_request_id, "request.json")
    ref = _publish_json(
        paths,
        session_id=state["session_id"],
        destination=destination,
        payload=record,
        stage=stage,
        method_name=method_name or f"{stage}.review.{gate_id}",
        artifact_type=artifact_type or f"{stage}.review_request",
        metadata={"review_request_id": review_request_id},
    )
    return PersistedReviewRequest(review_request_id=review_request_id, request_ref=ref)


def persist_review_recommendation(
    data_dir: str | Path,
    *,
    session_id: str,
    stage: str,
    review_request_id: str,
    request_ref: ArtifactRef | Mapping[str, Any],
    request_hash: str,
    payload: Mapping[str, Any],
) -> ArtifactRef:
    """Publish one immutable recommendation bound to one canonical request."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    _safe_component(review_request_id, "review_request_id")
    ref = request_ref if isinstance(request_ref, ArtifactRef) else ArtifactRef.model_validate(request_ref)
    if request_hash != ref.sha256:
        raise ReviewPersistenceError(
            "review recommendation request_hash does not match request_ref.sha256"
        )
    request_record = load_review_request(
        paths.run_dir,
        ref,
        session_id=session_id,
    )
    if request_record.get("review_request_id") != review_request_id:
        raise ReviewPersistenceError(
            "review recommendation ID does not match the canonical request"
        )
    if request_record.get("stage") != stage:
        raise ReviewPersistenceError(
            "review recommendation stage does not match the canonical request"
        )

    recommendation_body = dict(payload)
    reserved = {
        "schema_version",
        "review_request_id",
        "request_ref",
        "request_hash",
        "review_request",
    }
    duplicated = sorted(reserved & set(recommendation_body))
    if duplicated:
        raise ReviewPersistenceError(
            "review recommendation payload contains reserved fields: "
            + ", ".join(duplicated)
        )
    record = {
        "schema_version": 2,
        "review_request_id": review_request_id,
        "request_ref": ref.model_dump(mode="json"),
        "request_hash": request_hash,
        **_portable_value(paths, recommendation_body),
    }
    destination = paths.artifact_path(
        "reviews",
        review_request_id,
        "recommendation.json",
    )
    return _publish_json(
        paths,
        session_id=session_id,
        destination=destination,
        payload=record,
        stage=stage,
        method_name=f"{stage}.review_recommender",
        artifact_type=f"{stage}.review_recommendation",
        metadata={
            "review_request_id": review_request_id,
            "decided_by": "llm_provisional",
        },
    )


def persist_review_decision(
    data_dir: str | Path,
    *,
    session_id: str,
    review_request_id: str,
    stage: str,
    gate_id: str,
    decision: Mapping[str, Any] | Any,
    request_hash: str | None = None,
    request_ref: ArtifactRef | Mapping[str, Any] | None = None,
    method_name: str | None = None,
    artifact_type: str | None = None,
) -> ArtifactRef:
    """Publish the sole immutable decision bound to a review request."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    _safe_component(review_request_id, "review_request_id")
    validated_request_ref = _validate_optional_ref(request_ref)
    if validated_request_ref is not None:
        _verify_ref(paths, validated_request_ref)
        if request_hash is not None and request_hash != validated_request_ref.sha256:
            raise ReviewPersistenceError("request_hash does not match request_ref.sha256")
        request_hash = validated_request_ref.sha256
    if request_hash is None or len(request_hash) != 64 or any(
        character not in "0123456789abcdef" for character in request_hash
    ):
        raise ReviewPersistenceError("decision requires the bound review request SHA256")
    record = {
        "schema_version": 2,
        "review_request_id": review_request_id,
        "stage": stage,
        "gate_id": gate_id,
        "request_hash": request_hash,
        "request_ref": (
            validated_request_ref.model_dump(mode="json")
            if validated_request_ref is not None
            else None
        ),
        "decision": _portable_value(paths, _dump_payload(decision)),
    }
    destination = paths.artifact_path("reviews", review_request_id, "decision.json")
    return _publish_json(
        paths,
        session_id=session_id,
        destination=destination,
        payload=record,
        stage=stage,
        method_name=method_name or f"{stage}.review.{gate_id}",
        artifact_type=artifact_type or f"{stage}.review_decision",
        metadata={"review_request_id": review_request_id},
    )


def load_review_request(
    data_dir: str | Path,
    request_ref: ArtifactRef | Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Verify and load a canonical review-request JSON record."""

    paths = SessionPaths.from_run_dir(data_dir)
    if session_id is not None:
        paths.require_session_id(session_id)
    ref = request_ref if isinstance(request_ref, ArtifactRef) else ArtifactRef.model_validate(request_ref)
    path = _verify_ref(paths, ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewPersistenceError(f"review request is unreadable: {ref.path_ref}") from exc
    if not isinstance(payload, dict):
        raise ReviewPersistenceError(f"invalid review request record: {ref.path_ref}")
    _validate_review_request_record(payload, ref.path_ref)
    return payload


def _paths_for_state(state: TopLevelState) -> SessionPaths:
    paths = SessionPaths.from_run_dir(state.get("data_dir", ""))
    paths.require_session_id(state["session_id"])
    return paths


def _publish_json(
    paths: SessionPaths,
    *,
    session_id: str,
    destination: Path,
    payload: Mapping[str, Any],
    stage: str,
    method_name: str,
    artifact_type: str,
    metadata: dict[str, Any],
) -> ArtifactRef:
    temporary = paths.work_path(
        "reviews",
        "persistence",
        f".{destination.name}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(_pretty_bytes(payload))
    try:
        published = publish_file(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    artifact = ArtifactMeta.from_file(
        published,
        stage=stage,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata=metadata,
    )
    refs = append_artifact_records(paths.run_dir, session_id, [artifact])
    if len(refs) != 1:
        raise ReviewPersistenceError("artifact index did not return one review ref")
    return refs[0]


def _verify_ref(paths: SessionPaths, ref: ArtifactRef) -> Path:
    path = paths.resolve_ref(ref.path_ref)
    try:
        size = path.stat().st_size
        digest = file_sha256(path)
    except OSError as exc:
        raise ReviewPersistenceError(f"review artifact is missing: {ref.path_ref}") from exc
    if size != ref.size_bytes or digest != ref.sha256:
        raise ReviewPersistenceError(f"review artifact integrity mismatch: {ref.path_ref}")
    return path


def _validate_optional_ref(
    value: ArtifactRef | Mapping[str, Any] | None,
) -> ArtifactRef | None:
    if value is None:
        return None
    return value if isinstance(value, ArtifactRef) else ArtifactRef.model_validate(value)


def _portable_value(paths: SessionPaths, value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _portable_value(paths, value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _portable_value(paths, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_value(paths, item) for item in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            try:
                return paths.to_ref(candidate)
            except DataLayoutError:
                return value
    return value


def _dump_payload(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump(mode="json")
        fields_set = getattr(payload, "model_fields_set", set())
        if dumped.get("gate_id") is None and "gate_id" not in fields_set:
            dumped.pop("gate_id", None)
    else:
        dumped = dict(payload)
    if not isinstance(dumped, dict):
        raise ReviewPersistenceError("review payload must be a JSON object")
    return dumped


def _review_request_body(
    payload: Mapping[str, Any],
    *,
    stage: str,
    gate_id: str,
    reason: str,
) -> dict[str, Any]:
    """Validate envelope duplicates, then return the gate-specific body only."""

    body = dict(payload)
    expected = {
        "stage": stage,
        "gate_id": gate_id,
        "reason": reason,
    }
    for key, expected_value in expected.items():
        if key not in body:
            continue
        actual = body.pop(key)
        if actual != expected_value:
            raise ReviewPersistenceError(
                f"review request payload {key} does not match canonical envelope"
            )
    return body


def _validate_review_request_record(record: Mapping[str, Any], path_ref: str) -> None:
    if record.get("schema_version") != 2:
        raise ReviewPersistenceError(f"invalid review request record: {path_ref}")
    stage = record.get("stage")
    gate_id = record.get("gate_id")
    reason = record.get("reason")
    bindings = record.get("bindings")
    body = record.get("payload")
    request_id = record.get("review_request_id")
    if not isinstance(stage, str) or not isinstance(gate_id, str) or not isinstance(reason, str):
        raise ReviewPersistenceError(f"invalid review request envelope: {path_ref}")
    if not isinstance(bindings, dict) or not isinstance(body, dict):
        raise ReviewPersistenceError(f"invalid review request body: {path_ref}")
    duplicated = sorted({"stage", "gate_id", "reason"} & set(body))
    if duplicated:
        raise ReviewPersistenceError(
            "review request body duplicates canonical envelope fields: "
            + ", ".join(duplicated)
        )
    identity_payload = {
        "schema_version": 2,
        "stage": stage,
        "gate_id": gate_id,
        "reason": reason,
        "bindings": bindings,
        "payload": body,
    }
    expected_id = f"review_{sha256(_canonical_bytes(identity_payload)).hexdigest()[:24]}"
    if request_id != expected_id:
        raise ReviewPersistenceError(
            f"review request ID does not match canonical content: {path_ref}"
        )


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pretty_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _safe_component(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise ReviewPersistenceError(f"{label} must be one safe path component")
    return value
