"""Small JSONL ledger for business execution facts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas.descriptors import FrozenAttemptSpec


class Ledger:
    """Session-local ledger stored under data_dir/ledger."""

    def __init__(self, data_dir: Path | str = "data") -> None:
        self.data_dir = Path(data_dir)

    def session_dir(self, session_id: str) -> Path:
        """Return the ledger directory for a session."""
        return self.data_dir / "ledger" / session_id

    def create_session(self, session_id: str, input_manifest_ref: str) -> None:
        """Create session metadata if it does not already exist."""
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "session.json"
        if not session_path.exists():
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "input_manifest_ref": input_manifest_ref,
                        "created_at": _now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
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
        """Append an attempt record."""
        self.record_event(
            session_id,
            "attempt_recorded",
            {
                "attempt_id": attempt.attempt_id,
                "stage": attempt.stage,
                "method_name": attempt.method_name,
                "status": status,
                "script_path": str(attempt.script_path),
                "working_dir": str(attempt.working_dir),
                "artifacts": artifacts or [],
                "error": error,
            },
        )

    def record_attempt_started(self, session_id: str, attempt: FrozenAttemptSpec) -> None:
        """Append an attempt-start event."""
        self.record_event(
            session_id,
            "attempt_started",
            {
                "attempt_id": attempt.attempt_id,
                "stage": attempt.stage,
                "method_name": attempt.method_name,
                "working_dir": str(attempt.working_dir),
            },
        )

    def record_attempt_succeeded(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an attempt-success event."""
        self.record_event(
            session_id,
            "attempt_succeeded",
            {
                "attempt_id": attempt.attempt_id,
                "stage": attempt.stage,
                "method_name": attempt.method_name,
                "artifacts": artifacts or [],
            },
        )

    def record_attempt_failed(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        return_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """Append an attempt-failure event."""
        self.record_event(
            session_id,
            "attempt_failed",
            {
                "attempt_id": attempt.attempt_id,
                "stage": attempt.stage,
                "method_name": attempt.method_name,
                "return_code": return_code,
                "error": error,
            },
        )

    def record_artifact_registered(
        self,
        session_id: str,
        artifact: dict[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> None:
        """Append an artifact-registration event."""
        self.record_event(
            session_id,
            "artifact_registered",
            {
                "attempt_id": attempt_id,
                "artifact": artifact,
            },
        )

    def record_assets_registered(
        self,
        session_id: str,
        assets: list[dict[str, Any]],
        *,
        registry_ref: str,
        ambiguous: bool = False,
    ) -> None:
        """Append an asset-registration event."""
        self.record_event(
            session_id,
            "assets_registered",
            {
                "registry_ref": registry_ref,
                "asset_count": len(assets),
                "assets": assets,
                "ambiguous": ambiguous,
            },
        )

    def record_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Append a business event."""
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": event_type, "timestamp": _now(), **payload}) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
