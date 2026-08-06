"""Append-only Data Architecture v2 lifecycle ledger.

The ledger owns causal ordering only.  Evidence bodies live in immutable
artifacts and are referenced from the bounded event envelope written here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.artifacts import read_artifact_records
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout

LEDGER_SCHEMA_VERSION = 2
SUMMARY_MAX_BYTES = 12 * 1024
MAX_REFS = 64
MAX_LIST_ITEMS = 32
MAX_TEXT_CHARS = 4096

_COLLECTION_FACTS = frozenset({"artifacts", "assets", "candidates", "signals"})
_RESERVED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "sequence",
        "event_id",
        "idempotency_key",
        "session_id",
        "timestamp",
        "event_type",
        "workflow_step_id",
        "subject_id",
    }
)
_REF_KEYS = frozenset(
    {
        "artifact_ref",
        "blocked_input_ref",
        "bundle_ref",
        "comparison_artifact_ref",
        "decision_artifact_ref",
        "draft_assembly_ref",
        "input_manifest_ref",
        "rationale_artifact_ref",
        "registry_ref",
        "review_request_artifact_ref",
    }
)
_OCCURRENCE_EVENT_TYPES = frozenset(
    {
        "artifact_registered",
        "assets_registered",
        "attempt_recorded",
        "attempt_started",
        "attempt_succeeded",
        "attempt_failed",
        "review_requested",
        "review_decision",
        "stage_entered",
        "stage_skipped",
        "stage_completed",
        "stage_failed",
    }
)


class LedgerFormatError(ValueError):
    """Raised when a ledger contains a non-v2 or malformed row."""


class LedgerConflictError(RuntimeError):
    """Raised when one idempotency key is reused for different facts."""


class Ledger:
    """One self-contained run's append-only causal event stream."""

    def __init__(self, data_dir: Path | str) -> None:
        self.paths = SessionPaths.from_run_dir(data_dir)
        self.data_dir = self.paths.run_dir

    @property
    def events_path(self) -> Path:
        return self.paths.ledger_events_path

    def session_dir(self, session_id: str) -> Path:
        """Return the run-local ledger directory after identity validation."""
        self.paths.require_session_id(session_id)
        return self.paths.ledger_dir

    def create_session(self, session_id: str, input_manifest_ref: str) -> None:
        """Idempotently record creation in the event stream.

        V2 deliberately has no ``ledger/session.json``.  ``session_created`` is
        the sole durable owner of the session creation timestamp and manifest
        reference, and :meth:`read_session` is its read-model projection.
        """
        self.paths.require_session_id(session_id)
        refs = []
        if input_manifest_ref:
            refs.append(self._path_ref("input_manifest_ref", input_manifest_ref))
        self.record_event(
            session_id,
            "session_created",
            {},
            subject_id=session_id,
            refs=refs,
            idempotency_key=f"session:{session_id}:created",
        )

    def record_attempt(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        status: str,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        """Record an attempt status without copying its execution manifest."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_recorded",
            {
                "method_name": attempt.method_name,
                "status": status,
                "error": error,
            },
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            refs=self._artifact_refs(artifacts or []),
            idempotency_key=f"attempt:{attempt.attempt_id}:recorded:{status}",
        )

    def record_attempt_started(
        self, session_id: str, attempt: FrozenAttemptSpec
    ) -> None:
        """Append an idempotent attempt-start event."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_started",
            {"method_name": attempt.method_name},
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            idempotency_key=f"attempt:{attempt.attempt_id}:started",
        )

    def record_attempt_succeeded(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an attempt-success event referring to its output evidence."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_succeeded",
            {"method_name": attempt.method_name},
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            refs=self._artifact_refs(artifacts or []),
            idempotency_key=f"attempt:{attempt.attempt_id}:succeeded",
        )

    def record_attempt_failed(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        return_code: int | None = None,
        error: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an attempt-failure event with a bounded diagnostic summary."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_failed",
            {
                "method_name": attempt.method_name,
                "return_code": return_code,
                "error": error,
            },
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            refs=self._artifact_refs(artifacts or []),
            idempotency_key=f"attempt:{attempt.attempt_id}:failed",
        )

    def has_attempt_success(self, session_id: str, attempt_id: str) -> bool:
        """Return True when the attempt already owns a terminal success event.

        Collect adapters own ``attempt_succeeded``; the shared failure
        boundary records ``attempt_failed`` only while the attempt has not
        already succeeded, so one attempt never carries both terminal facts.
        """
        return any(
            row["event_type"] == "attempt_succeeded" and row["subject_id"] == attempt_id
            for row in self.read_event_rows(session_id)
        )

    def record_artifact_registered(
        self,
        session_id: str,
        artifact: Any,
        *,
        attempt_id: str | None = None,
    ) -> None:
        """Record the causal registration without copying index metadata."""
        artifact_record = _as_mapping(artifact)
        ref = self._artifact_ref(artifact_record)
        indexed_record = self._indexed_artifact_record(ref)
        workflow_step_id = _require_workflow_step_id(
            indexed_record.get("workflow_step_id")
        )
        indexed_stage = indexed_record.get("stage")
        if not isinstance(indexed_stage, str) or not indexed_stage:
            raise DataLayoutError("artifact index record has no stage")
        supplied_stage = _optional_text(artifact_record.get("stage"))
        if supplied_stage is not None and supplied_stage != indexed_stage:
            raise DataLayoutError("artifact stage does not match its artifact index")
        artifact_id = str(ref.get("artifact_id") or ref.get("path_ref") or "unknown")
        self.record_event(
            session_id,
            "artifact_registered",
            {"attempt_id": attempt_id},
            stage=indexed_stage,
            workflow_step_id=workflow_step_id,
            subject_id=artifact_id,
            refs=[ref],
            idempotency_key=f"artifact:{artifact_id}:registered",
        )

    def record_assets_registered(
        self,
        session_id: str,
        assets: list[dict[str, Any]],
        *,
        registry_ref: Any,
        ambiguous: bool = False,
        workflow_step_id: str,
    ) -> None:
        """Record inventory cardinality and the authoritative registry ref."""
        registry = self._path_ref("registry_ref", registry_ref)
        subject_id = str(registry.get("path_ref") or registry_ref)
        self.record_event(
            session_id,
            "assets_registered",
            {"asset_count": len(assets), "ambiguous": ambiguous},
            stage="inventory",
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=subject_id,
            refs=[registry],
            idempotency_key=f"asset-registry:{subject_id}:registered",
        )

    def record_review_requested(
        self,
        session_id: str,
        stage: str,
        *,
        reason: str,
        signals: list[dict[str, Any]] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        review_request_artifact_ref: Any,
        auto_decidable: bool | None = None,
        review_request_id: str | None = None,
    ) -> None:
        """Record a request by ID; full signals/candidates remain in its artifact."""
        request_ref = self._path_ref(
            "review_request_artifact_ref", review_request_artifact_ref
        )
        request_record = self._indexed_artifact_record(request_ref)
        workflow_step_id = _require_workflow_step_id(
            request_record.get("workflow_step_id")
        )
        if request_record.get("stage") != stage:
            raise DataLayoutError("review request stage does not match its artifact")
        indexed_request_id = _require_review_request_id(request_record)
        if review_request_id is not None and review_request_id != indexed_request_id:
            raise DataLayoutError("review request ID does not match its artifact index")
        request_id = indexed_request_id
        payload: dict[str, Any] = {
            "reason": reason,
            "signal_count": len(signals or []),
            "candidate_count": len(candidates or []),
            "review_request_id": request_id,
        }
        if auto_decidable is not None:
            payload["auto_decidable"] = auto_decidable
        self.record_event(
            session_id,
            "review_requested",
            payload,
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=request_id,
            refs=[request_ref],
            idempotency_key=f"review:{request_id}:requested",
        )

    def record_review_decision(
        self,
        session_id: str,
        stage: str,
        *,
        decision: str,
        gate_id: str | None = None,
        candidate_id: str | None = None,
        rejected_candidates: list[str] | None = None,
        notes: list[str] | None = None,
        method_name: str | None = None,
        artifact_ref: Any = None,
        metadata: dict[str, Any] | None = None,
        decided_by: str = "human",
        rationale_artifact_ref: Any = None,
        review_request_id: str | None = None,
    ) -> None:
        """Record a bounded decision bound to its indexed request occurrence."""
        if artifact_ref is None:
            raise DataLayoutError("review decision requires its decision artifact ref")
        decision_ref = self._path_ref("artifact_ref", artifact_ref)
        decision_record = self._indexed_artifact_record(decision_ref)
        workflow_step_id = _require_workflow_step_id(
            decision_record.get("workflow_step_id")
        )
        if decision_record.get("stage") != stage:
            raise DataLayoutError("review decision stage does not match its artifact")
        indexed_request_id = _require_review_request_id(decision_record)
        if review_request_id is not None and review_request_id != indexed_request_id:
            raise DataLayoutError(
                "review decision request ID does not match its artifact"
            )
        request_id = indexed_request_id
        refs: list[dict[str, Any]] = [decision_ref]
        if rationale_artifact_ref:
            refs.append(
                self._path_ref("rationale_artifact_ref", rationale_artifact_ref)
            )
        payload = {
            "review_request_id": request_id,
            "gate_id": gate_id,
            "decision": decision,
            "candidate_id": candidate_id,
            "rejected_candidates": rejected_candidates or [],
            "rejected_candidate_count": len(rejected_candidates or []),
            "note_count": len(notes or []),
            "method_name": method_name,
            "decided_by": decided_by,
            "metadata": metadata or {},
        }
        self.record_event(
            session_id,
            "review_decision",
            payload,
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=request_id,
            refs=refs,
            idempotency_key=f"review:{request_id}:decision",
        )

    def record_stage_entered(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        route_node: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "stage_entered",
            {"route_node": route_node},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            idempotency_key=idempotency_key or f"stage-entered:{workflow_step_id}",
        )

    def record_stage_skipped(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        reason: str,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "stage_skipped",
            {"reason": reason},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-skipped:{workflow_step_id}",
        )

    def record_stage_completed(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "stage_completed",
            {},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-completed:{workflow_step_id}",
        )

    def record_stage_failed(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        error: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "stage_failed",
            {"error": error},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-failed:{workflow_step_id}",
        )

    def record_session_terminal(
        self,
        session_id: str,
        *,
        outcome: str,
        stage: str | None = None,
        error: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "session_terminal",
            {"outcome": outcome, "error": error},
            stage=stage,
            subject_id=session_id,
            refs=refs,
            idempotency_key=idempotency_key
            or f"session:{session_id}:terminal:{outcome}",
        )

    def read_session(self, session_id: str) -> dict[str, Any] | None:
        """Project session metadata from ``session_created``."""
        self.paths.require_session_id(session_id)
        created = next(
            (
                row
                for row in self.read_events(session_id)
                if row["event_type"] == "session_created"
            ),
            None,
        )
        if created is None:
            return None
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "session_id": session_id,
            "input_manifest_ref": created.get("input_manifest_ref"),
            "created_at": created.get("timestamp"),
            "created_event_id": created.get("event_id"),
        }

    def read_event_rows(self, session_id: str) -> list[dict[str, Any]]:
        """Read and validate the exact v2 rows stored on disk."""
        self.paths.require_session_id(session_id)
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerFormatError(
                        f"invalid JSON in {self.events_path}:{line_number}"
                    ) from exc
                self._validate_row(row, line_number)
                rows.append(row)
        return rows

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        """Return bounded query projections while preserving the v2 envelope."""
        return [self._project_row(row) for row in self.read_event_rows(session_id)]

    def last_sequence(self, session_id: str) -> int:
        rows = self.read_event_rows(session_id)
        return int(rows[-1]["sequence"]) if rows else 0

    def record_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        stage: str | None = None,
        workflow_step_id: str | None = None,
        subject_id: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append one idempotent, bounded v2 event and return its exact row."""
        self.paths.require_session_id(session_id)
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event_type must be a non-empty string")
        ensure_run_layout(self.paths)

        raw = dict(payload or {})
        reserved = sorted(set(raw) & _RESERVED_PAYLOAD_KEYS)
        if reserved:
            raise ValueError(
                "ledger payload cannot override envelope fields: " + ", ".join(reserved)
            )
        stage = stage or _optional_text(raw.pop("stage", None))
        if workflow_step_id is None:
            workflow_step_id = _optional_text(raw.pop("workflow_step_id", None))
        else:
            raw.pop("workflow_step_id", None)
        if event_type in _OCCURRENCE_EVENT_TYPES:
            workflow_step_id = _require_workflow_step_id(workflow_step_id)
        elif workflow_step_id is not None:
            workflow_step_id = _require_workflow_step_id(workflow_step_id)
        subject_id = subject_id or _subject_from_payload(raw)
        explicit_refs = list(refs or [])
        payload_refs, summary_input = self._split_payload(raw)
        normalized_refs = self._normalize_refs([*explicit_refs, *payload_refs])
        summary = _bounded_summary(summary_input)
        semantic = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "session_id": session_id,
            "event_type": event_type,
            "stage": stage,
            "workflow_step_id": workflow_step_id,
            "subject_id": subject_id,
            "refs": normalized_refs,
            "summary": summary,
        }
        key = idempotency_key or _default_idempotency_key(semantic)
        if len(key) > 256:
            key = f"idem_{_sha256_text(key)}"

        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = self._rows_from_locked_handle(handle)
                for existing in rows:
                    if existing["idempotency_key"] != key:
                        continue
                    if _semantic_body(existing) != semantic:
                        raise LedgerConflictError(
                            f"idempotency key {key!r} already owns a different "
                            "ledger fact"
                        )
                    return existing

                sequence = int(rows[-1]["sequence"]) + 1 if rows else 1
                row = {
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "sequence": sequence,
                    "event_id": f"evt_{sequence:08d}_{uuid4().hex[:12]}",
                    "idempotency_key": key,
                    "session_id": session_id,
                    "timestamp": _now(),
                    "event_type": event_type,
                    "stage": stage,
                    "workflow_step_id": workflow_step_id,
                    "subject_id": subject_id,
                    "refs": normalized_refs,
                    "summary": summary,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
                return row
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def references_from(
        self,
        value: Any,
        *,
        role_prefix: str,
    ) -> list[dict[str, Any]]:
        """Extract normalized run-local refs from a compact nested value."""

        return self._normalize_refs(
            self._refs_from_nested(value, role_prefix=role_prefix)
        )

    def _rows_from_locked_handle(self, handle: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerFormatError(
                    f"invalid JSON in {self.events_path}:{line_number}"
                ) from exc
            self._validate_row(row, line_number)
            rows.append(row)
        return rows

    def _validate_row(self, row: Any, line_number: int) -> None:
        required = {
            "schema_version",
            "sequence",
            "event_id",
            "idempotency_key",
            "session_id",
            "timestamp",
            "event_type",
            "stage",
            "workflow_step_id",
            "subject_id",
            "refs",
            "summary",
        }
        if not isinstance(row, dict) or not required.issubset(row):
            raise LedgerFormatError(f"malformed v2 ledger row at line {line_number}")
        if row["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise LedgerFormatError(
                f"unsupported ledger schema at line {line_number}: "
                f"{row['schema_version']!r}"
            )
        if row["session_id"] != self.paths.session_id:
            raise LedgerFormatError(
                f"session mismatch in ledger row at line {line_number}"
            )
        if not isinstance(row["sequence"], int) or row["sequence"] < 1:
            raise LedgerFormatError(
                f"invalid sequence in ledger row at line {line_number}"
            )
        if not isinstance(row["refs"], list) or not isinstance(row["summary"], dict):
            raise LedgerFormatError(
                f"invalid refs/summary in ledger row at line {line_number}"
            )
        workflow_step_id = row["workflow_step_id"]
        if workflow_step_id is not None and (
            not isinstance(workflow_step_id, str) or not workflow_step_id
        ):
            raise LedgerFormatError(
                f"invalid workflow_step_id in ledger row at line {line_number}"
            )
        if (
            row["event_type"] in _OCCURRENCE_EVENT_TYPES
            and not isinstance(workflow_step_id, str)
        ):
            raise LedgerFormatError(
                f"occurrence event lacks workflow_step_id at line {line_number}"
            )
        if _json_size(row["summary"]) > SUMMARY_MAX_BYTES:
            raise LedgerFormatError(
                f"oversized summary in ledger row at line {line_number}"
            )

    def _split_payload(
        self, payload: dict[str, Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        refs: list[Any] = []
        summary: dict[str, Any] = {}
        supplied_refs = payload.pop("refs", None)
        if isinstance(supplied_refs, list):
            refs.extend(supplied_refs)
        supplied_summary = payload.pop("summary", None)
        if isinstance(supplied_summary, Mapping):
            summary.update(dict(supplied_summary))

        for key, value in payload.items():
            if key == "artifact" and isinstance(value, Mapping):
                refs.append({**dict(value), "role": "artifact"})
                continue
            if key in _COLLECTION_FACTS:
                if key == "artifacts" and isinstance(value, list):
                    for item in value:
                        if hasattr(item, "model_dump"):
                            item = item.model_dump(mode="json")
                        if isinstance(item, Mapping):
                            refs.append({**dict(item), "role": "artifact"})
                summary[f"{key[:-1]}_count"] = (
                    len(value) if isinstance(value, list) else 0
                )
                continue
            if _is_asset_ref_key(key) and value:
                refs.extend(self._asset_value_refs(key, value))
                continue
            if _is_ref_key(key) and value:
                refs.append(self._path_ref(key, value))
                continue
            if key == "relevant_refs":
                extracted = self._refs_from_nested(value, role_prefix="relevant")
                refs.extend(extracted)
                summary["relevant_ref_count"] = len(extracted)
                continue
            summary[key] = value
        return refs, summary

    def _refs_from_nested(
        self, value: Any, *, role_prefix: str
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        if isinstance(value, Mapping):
            for key, nested in value.items():
                role = f"{role_prefix}.{key}"
                key_text = str(key)
                if _is_asset_ref_key(key_text):
                    refs.extend(self._asset_value_refs(role, nested))
                elif isinstance(nested, (str, Path)) and _is_ref_key(key_text):
                    try:
                        refs.append(self._path_ref(role, nested))
                    except DataLayoutError:
                        if Path(nested).is_absolute():
                            continue
                        raise
                else:
                    refs.extend(self._refs_from_nested(nested, role_prefix=role))
        elif isinstance(value, list):
            for index, nested in enumerate(value[:MAX_REFS]):
                refs.extend(
                    self._refs_from_nested(nested, role_prefix=f"{role_prefix}.{index}")
                )
        return refs

    def _asset_value_refs(self, role: str, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, (str, Path)):
            return [{"role": role, "value": _bounded_scalar(value)}]
        if isinstance(value, (list, tuple)):
            return [
                {
                    "role": f"{role}.{index}",
                    "value": _bounded_scalar(nested),
                }
                for index, nested in enumerate(value[:MAX_REFS])
                if isinstance(nested, (str, Path)) and nested
            ]
        return []

    def _normalize_refs(self, refs: Iterable[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in refs:
            if len(normalized) >= MAX_REFS:
                break
            ref = self._normalize_ref(value)
            if ref is None:
                continue
            identity = json.dumps(
                ref, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity in seen:
                continue
            normalized.append(ref)
            seen.add(identity)
        return normalized

    def _normalize_ref(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, (str, Path)):
            return self._path_ref("artifact_ref", value)
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, Mapping):
            return None
        ref = dict(value)
        role = _optional_text(ref.get("role"))
        path_value = ref.get("path_ref") or ref.get("file_path")
        normalized: dict[str, Any] = {}
        if role:
            normalized["role"] = role
        for key in ("artifact_id", "sha256", "artifact_type"):
            if ref.get(key) is not None:
                normalized[key] = _bounded_scalar(ref[key])
        if "sha256" not in normalized and ref.get("file_hash") is not None:
            normalized["sha256"] = _bounded_scalar(ref["file_hash"])
        size = ref.get("size_bytes")
        if size is None:
            size = ref.get("file_size_bytes")
        if size is not None:
            normalized["size_bytes"] = int(size)
        if path_value:
            normalized["path_ref"] = self._normalize_path_ref(path_value)
        elif ref.get("value") is not None:
            normalized["value"] = _bounded_scalar(ref["value"])
        if normalized.get("role") == "artifact" and normalized.get("path_ref"):
            normalized = self._indexed_artifact_ref(normalized)
        return normalized or None

    def _indexed_artifact_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        path_ref = str(ref["path_ref"])
        record = next(
            (
                candidate
                for candidate in read_artifact_records(
                    self.paths.run_dir,
                    self.paths.session_id,
                )
                if candidate.get("path_ref") == path_ref
            ),
            None,
        )
        if record is None:
            return ref

        supplied_id = ref.get("artifact_id")
        if (
            isinstance(supplied_id, str)
            and supplied_id.startswith("art_")
            and supplied_id != record["artifact_id"]
        ):
            raise DataLayoutError(
                "canonical artifact_id conflicts with the artifact index: "
                f"artifact_id={supplied_id}; path_ref={path_ref}; "
                f"indexed_artifact_id={record['artifact_id']}"
            )
        for key in ("sha256", "size_bytes", "artifact_type"):
            if key in ref and ref[key] != record[key]:
                raise DataLayoutError(
                    f"artifact {key} conflicts with the artifact index: "
                    f"artifact_id={supplied_id}; path_ref={path_ref}; "
                    f"expected={record[key]}; actual={ref[key]}"
                )
        return {
            "role": "artifact",
            "artifact_id": record["artifact_id"],
            "sha256": record["sha256"],
            "artifact_type": record["artifact_type"],
            "size_bytes": record["size_bytes"],
            "path_ref": record["path_ref"],
        }

    def _path_ref(self, role: str, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, Mapping):
            normalized = self._normalize_ref({**dict(value), "role": role})
            if normalized is None or not normalized.get("path_ref"):
                raise ValueError(f"{role} must contain a path_ref")
            return normalized
        return {"role": role, "path_ref": self._normalize_path_ref(value)}

    def _normalize_path_ref(self, value: str | Path) -> str:
        path = Path(value)
        if path.is_absolute():
            return self.paths.to_ref(path)
        resolved = self.paths.resolve_ref(path)
        return self.paths.to_ref(resolved)

    def _artifact_ref(self, artifact: Any) -> dict[str, Any]:
        return self._normalize_ref({**_as_mapping(artifact), "role": "artifact"}) or {}

    def _artifact_refs(self, artifacts: Iterable[Any]) -> list[dict[str, Any]]:
        return [self._artifact_ref(artifact) for artifact in artifacts]

    def _workflow_step_id_for_artifact_ref(self, ref: Mapping[str, Any]) -> str:
        record = self._indexed_artifact_record(ref)
        return _require_workflow_step_id(record.get("workflow_step_id"))

    def _review_request_id_for_artifact_ref(self, ref: Mapping[str, Any]) -> str:
        return _require_review_request_id(self._indexed_artifact_record(ref))

    def _indexed_artifact_record(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        path_ref = ref.get("path_ref")
        if not isinstance(path_ref, str) or not path_ref:
            raise DataLayoutError("artifact ref has no path_ref")
        record = next(
            (
                candidate
                for candidate in read_artifact_records(
                    self.paths.run_dir, self.paths.session_id
                )
                if candidate.get("path_ref") == path_ref
            ),
            None,
        )
        if record is None:
            raise DataLayoutError(
                f"artifact ref is not present in the index: {path_ref}"
            )
        return record

    def _latest_open_review_request_id(
        self, stage: str, workflow_step_id: str
    ) -> str | None:
        events = self.read_events(self.paths.session_id)
        decided = {
            str(event.get("review_request_id") or event.get("subject_id"))
            for event in events
            if event.get("event_type") == "review_decision"
        }
        for event in reversed(events):
            if (
                event.get("event_type") != "review_requested"
                or event.get("stage") != stage
                or event.get("workflow_step_id") != workflow_step_id
            ):
                continue
            request_id = str(event.get("review_request_id") or event.get("subject_id"))
            if request_id not in decided:
                return request_id
        return None

    @staticmethod
    def _project_row(row: dict[str, Any]) -> dict[str, Any]:
        projected = dict(row)
        projected.update(row.get("summary") or {})
        if row.get("subject_id"):
            event_type = row.get("event_type")
            if str(event_type).startswith("attempt_"):
                projected.setdefault("attempt_id", row["subject_id"])
            if event_type in {"review_requested", "review_decision"}:
                projected.setdefault("review_request_id", row["subject_id"])
        for ref in row.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            role = ref.get("role")
            value = ref.get("path_ref") or ref.get("value")
            if role == "artifact":
                projected.setdefault("artifact", ref)
                projected.setdefault("artifacts", []).append(ref)
            elif isinstance(role, str) and value is not None and "." not in role:
                projected.setdefault(role, value)
        return projected


def _semantic_body(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "schema_version",
            "session_id",
            "event_type",
            "stage",
            "workflow_step_id",
            "subject_id",
            "refs",
            "summary",
        )
    }


def _default_idempotency_key(semantic: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return f"event:{semantic['event_type']}:{digest[:32]}"


def _subject_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in (
        "attempt_id",
        "review_request_id",
        "candidate_id",
        "gate_id",
        "artifact_id",
    ):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _bounded_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        bounded = _bounded_value(str(key), value)
        if bounded is None:
            continue
        candidate[str(key)] = bounded
    if _json_size(candidate) <= SUMMARY_MAX_BYTES:
        return candidate

    compact: dict[str, Any] = {
        "summary_truncated": True,
        "summary_sha256": _sha256_text(
            json.dumps(
                candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        ),
    }
    omitted: list[str] = []
    priority = (
        "outcome",
        "status",
        "reason",
        "decision",
        "gate_id",
        "method_name",
        "return_code",
        "error",
        "current_stage",
        "supervision_state",
    )
    ordered = [key for key in priority if key in candidate]
    ordered.extend(key for key in candidate if key not in ordered)
    for key in ordered:
        trial = {**compact, key: candidate[key]}
        if _json_size(trial) <= SUMMARY_MAX_BYTES - 512:
            compact[key] = candidate[key]
        else:
            omitted.append(key)
    compact["omitted_fields"] = omitted[:MAX_LIST_ITEMS]
    return compact


def _bounded_value(key: str, value: Any) -> Any:
    if isinstance(value, (str, Path)):
        limit = (
            MAX_TEXT_CHARS
            if key in {"error", "detail", "reason", "last_error"}
            else 1024
        )
        text = str(value)
        return text if len(text) <= limit else text[: limit - 1] + "…"
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if key in {"notes", "signals", "candidates", "assets", "artifacts"}:
            return len(value)
        limit = ROUTE_TRACE_LIMIT if key == "route_trace" else MAX_LIST_ITEMS
        items = value[-limit:] if key == "route_trace" else value[:limit]
        if all(
            isinstance(item, (str, bool, int, float, Path)) or item is None
            for item in value
        ):
            return [_bounded_scalar(item) for item in items if item is not None]
        ids = [
            _bounded_scalar(
                item.get("candidate_id") or item.get("artifact_id") or item.get("id")
            )
            for item in items
            if isinstance(item, Mapping)
            and (item.get("candidate_id") or item.get("artifact_id") or item.get("id"))
        ]
        return {"item_count": len(value), "ids": ids}
    if isinstance(value, Mapping):
        if len(value) <= 8 and all(
            isinstance(nested, (str, bool, int, float, Path)) or nested is None
            for nested in value.values()
        ):
            return {
                str(nested_key): _bounded_scalar(nested)
                for nested_key, nested in value.items()
                if nested is not None
            }
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return {
            "item_count": len(value),
            "keys": [str(nested_key) for nested_key in list(value)[:MAX_LIST_ITEMS]],
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
    return _bounded_scalar(value)


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= 1024 else text[:1023] + "…"


def _is_ref_key(key: str) -> bool:
    return key in _REF_KEYS or key.endswith("_ref") or key.endswith("_artifact_ref")


def _is_asset_ref_key(key: str) -> bool:
    return key.endswith("_asset_ref") or key.endswith("_asset_refs")


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _require_workflow_step_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise DataLayoutError("workflow_step_id must be a non-empty string")
    return value


def _workflow_step_id_from_attempt(attempt: FrozenAttemptSpec) -> str:
    return _require_workflow_step_id((attempt.metadata or {}).get("workflow_step_id"))


def _require_review_request_id(record: Mapping[str, Any]) -> str:
    value = record.get("review_request_id")
    if not isinstance(value, str) or not value:
        raise DataLayoutError("review artifact has no indexed review_request_id")
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise TypeError(f"expected an artifact/ref mapping, got {type(value).__name__}")
    return dict(value)


def _reference_identity(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return str(
            value.get("artifact_id")
            or value.get("path_ref")
            or value.get("file_path")
            or ""
        )
    return str(value)


def _review_request_id_from_ref(ref: dict[str, Any] | None) -> str | None:
    if not ref or not ref.get("path_ref"):
        return None
    parts = Path(str(ref["path_ref"])).parts
    try:
        reviews_index = parts.index("reviews")
    except ValueError:
        return None
    if reviews_index + 1 >= len(parts):
        return None
    request_id = parts[reviews_index + 1]
    return request_id if request_id.startswith("review_") else None


def _stable_subject(*parts: str) -> str:
    return f"{parts[0]}_{_sha256_text(':'.join(parts))[0:20]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
