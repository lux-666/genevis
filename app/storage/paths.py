"""Data Architecture v2 paths for self-contained workflow runs.

The only supported runtime layout is ``<data-root>/runs/<session-id>``.
Every durable reference is relative to that run root; absolute and escaping
references are rejected at the storage boundary.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

LAYOUT_SCHEMA_VERSION = 2
_LAYOUT_FILENAME = "layout.json"
_LEGACY_RUNTIME_DIRS = frozenset(
    {"artifacts", "checkpoints", "control", "ledger", "reports", "work"}
)


class DataLayoutError(ValueError):
    """Raised when a path is not part of the supported v2 data layout."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise DataLayoutError("session_id must be a non-empty string")
    candidate = Path(session_id)
    if (
        candidate.is_absolute()
        or len(candidate.parts) != 1
        or session_id in {".", ".."}
    ):
        raise DataLayoutError(f"session_id must be one path component: {session_id!r}")
    return session_id


def _contained_path(base: Path, *parts: str | Path) -> Path:
    if not parts:
        return base
    relative = Path(*parts)
    if relative.is_absolute():
        raise DataLayoutError(f"absolute path component is not allowed: {relative}")
    candidate = (base / relative).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DataLayoutError(
            f"path escapes its run-owned directory: {relative}"
        ) from exc
    return candidate


@dataclass(frozen=True)
class SessionPaths:
    """The single path authority for one v2 workflow run."""

    data_root: Path
    session_id: str

    def __init__(self, data_root: str | Path, session_id: str) -> None:
        object.__setattr__(self, "data_root", _resolved(data_root))
        object.__setattr__(self, "session_id", _validate_session_id(session_id))

    @classmethod
    def from_run_dir(cls, run_dir: str | Path) -> "SessionPaths":
        """Construct paths from a strict ``<data-root>/runs/<session-id>`` root."""
        resolved = _resolved(run_dir)
        if resolved.parent.name != "runs" or not resolved.name:
            raise DataLayoutError(
                "run directory must have shape <data-root>/runs/<session-id>: "
                f"{resolved}"
            )
        paths = cls(resolved.parent.parent, resolved.name)
        if paths.run_dir != resolved:
            raise DataLayoutError(f"invalid run directory: {resolved}")
        return paths

    def require_session_id(self, session_id: str) -> None:
        """Fail when a caller's session ID disagrees with the run root."""
        if _validate_session_id(session_id) != self.session_id:
            raise DataLayoutError(
                f"session_id {session_id!r} does not match run root {self.session_id!r}"
            )

    @property
    def layout_path(self) -> Path:
        return self.data_root / _LAYOUT_FILENAME

    @property
    def run_dir(self) -> Path:
        return self.data_root / "runs" / self.session_id

    @property
    def control_dir(self) -> Path:
        return self.run_dir / "control"

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def reports_dir(self) -> Path:
        return self.run_dir / "reports"

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def ledger_dir(self) -> Path:
        return self.run_dir / "ledger"

    @property
    def checkpoints_dir(self) -> Path:
        return self.run_dir / "checkpoints"

    @property
    def run_metadata_path(self) -> Path:
        return self.control_dir / "run.json"

    @property
    def manifest_path(self) -> Path:
        return self.control_dir / "manifest.yaml"

    @property
    def input_manifest_path(self) -> Path:
        """The immutable manifest published by the freeze transaction.

        ``manifest_path`` is retained for the existing draft/materialization
        callers.  New frozen execution must use this explicitly named path so
        a mutable draft can never be mistaken for the execution contract.
        """
        return self.control_dir / "input_manifest.yaml"

    @property
    def manifest_draft_path(self) -> Path:
        """The mutable manifest projection owned by the intake workflow."""
        return self.control_dir / "manifest_draft.yaml"

    @property
    def tui_state_path(self) -> Path:
        """The authoritative mutable state for a pre-freeze TUI session."""
        return self.control_dir / "tui_state.json"

    @property
    def ask_plan_path(self) -> Path:
        return self.control_dir / "ask_plan.json"

    @property
    def run_config_path(self) -> Path:
        return self.control_dir / "run_config.json"

    @property
    def dependency_manifest_path(self) -> Path:
        return self.control_dir / "dependency_manifest.json"

    @property
    def frozen_inputs_path(self) -> Path:
        """The create-only commit marker for frozen control inputs."""
        return self.control_dir / "frozen_inputs.json"

    @property
    def execution_lock_path(self) -> Path:
        """Advisory ownership lock for one frozen execution attempt."""
        return self.control_dir / ".execution.lock"

    @property
    def artifact_index_path(self) -> Path:
        return self.artifacts_dir / "index.jsonl"

    @property
    def ledger_events_path(self) -> Path:
        return self.ledger_dir / "events.jsonl"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.checkpoints_dir / "checkpoints.sqlite"

    def stage_artifacts_dir(self, stage: str) -> Path:
        return _contained_path(
            self.artifacts_dir, _validate_path_component(stage, "stage")
        )

    def stage_work_dir(self, stage: str) -> Path:
        return _contained_path(self.work_dir, _validate_path_component(stage, "stage"))

    def artifact_path(self, stage: str, *parts: str | Path) -> Path:
        return _contained_path(self.stage_artifacts_dir(stage), *parts)

    def work_path(self, stage: str, *parts: str | Path) -> Path:
        return _contained_path(self.stage_work_dir(stage), *parts)

    def report_path(self, *parts: str | Path) -> Path:
        return _contained_path(self.reports_dir, *parts)

    def to_ref(self, path: str | Path) -> str:
        """Return a normalized run-root-relative reference."""
        candidate = _resolved(path)
        try:
            relative = candidate.relative_to(self.run_dir)
        except ValueError as exc:
            raise DataLayoutError(
                f"durable reference is outside run root: {candidate}"
            ) from exc
        if relative == Path("."):
            raise DataLayoutError(
                "a durable reference must identify an entry below the run root"
            )
        return relative.as_posix()

    def resolve_ref(self, path_ref: str | Path) -> Path:
        """Resolve a normalized reference while rejecting absolute/traversal paths."""
        if not str(path_ref).strip():
            raise DataLayoutError("path_ref must be non-empty")
        candidate = _contained_path(self.run_dir, path_ref)
        normalized_ref = candidate.relative_to(self.run_dir).as_posix()
        supplied_ref = Path(path_ref).as_posix()
        if supplied_ref != normalized_ref:
            raise DataLayoutError(f"path_ref must be normalized: {path_ref!s}")
        return candidate


def _validate_path_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataLayoutError(f"{label} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise DataLayoutError(f"{label} must be one path component: {value!r}")
    return value


def ensure_data_layout(data_root: str | Path) -> Path:
    """Create or validate the v2 root marker without adopting flat sessions."""
    root = _resolved(data_root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _LAYOUT_FILENAME
    if marker.exists():
        _read_layout_marker(marker)
        return marker

    legacy = sorted(name for name in _LEGACY_RUNTIME_DIRS if (root / name).exists())
    runs_dir = root / "runs"
    if runs_dir.is_dir() and any(runs_dir.iterdir()):
        legacy.append("runs/<unmarked-session>")
    if legacy:
        raise DataLayoutError(
            "refusing to mark a data root containing flat runtime directories as v2: "
            + ", ".join(legacy)
        )

    payload = {"schema_version": LAYOUT_SCHEMA_VERSION}
    temporary = root / f".{_LAYOUT_FILENAME}.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.link(temporary, marker)
    except FileExistsError:
        _read_layout_marker(marker)
    finally:
        temporary.unlink(missing_ok=True)
    return marker


def validate_data_layout(data_root: str | Path) -> dict[str, Any]:
    """Read and validate an existing v2 marker."""
    return _read_layout_marker(_resolved(data_root) / _LAYOUT_FILENAME)


def _read_layout_marker(marker: Path) -> dict[str, Any]:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataLayoutError(f"missing v2 data layout marker: {marker}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLayoutError(f"invalid data layout marker: {marker}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != LAYOUT_SCHEMA_VERSION
    ):
        raise DataLayoutError(
            f"unsupported data layout schema in {marker}; expected "
            f"{LAYOUT_SCHEMA_VERSION}"
        )
    return payload


def ensure_run_layout(paths: SessionPaths) -> Path:
    """Create the fixed directories owned by a v2 run."""
    ensure_data_layout(paths.data_root)
    for directory in (
        paths.control_dir,
        paths.ledger_dir,
        paths.checkpoints_dir,
        paths.artifacts_dir,
        paths.reports_dir,
        paths.work_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths.run_dir


def locate_session_data_dir(data_root: str | Path, session_id: str) -> Path | None:
    """Locate a v2 run; legacy flat sessions are intentionally invisible."""
    root = _resolved(data_root)
    if root.parent.name == "runs":
        paths = SessionPaths.from_run_dir(root)
        paths.require_session_id(session_id)
    else:
        paths = SessionPaths(root, session_id)
    if not paths.run_dir.is_dir():
        return None
    validate_data_layout(paths.data_root)
    return paths.run_dir


def is_session_run_dir(data_dir: str | Path, session_id: str) -> bool:
    """Whether ``data_dir`` is the strict v2 run root for ``session_id``."""
    try:
        paths = SessionPaths.from_run_dir(data_dir)
        paths.require_session_id(session_id)
    except DataLayoutError:
        return False
    return True


def artifact_index_path(data_dir: str | Path, session_id: str) -> Path:
    """Return the v2 artifact index, rejecting flat layout roots."""
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    return paths.artifact_index_path


def ledger_session_dir(data_dir: str | Path, session_id: str) -> Path:
    """Return the v2 ledger directory, rejecting flat layout roots."""
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    return paths.ledger_dir
