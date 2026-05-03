"""Artifact index helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.artifacts import ArtifactMeta


def append_artifact_records(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> Path:
    """Append artifact metadata to the session artifact index."""
    index_dir = Path(data_dir) / "artifacts" / session_id
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "artifacts.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        for artifact in artifacts:
            handle.write(json.dumps(artifact.model_dump(mode="json")) + "\n")
    return index_path
