"""Artifact index helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.artifacts import ArtifactMeta
from app.storage.paths import artifact_index_path, is_session_run_dir
from app.storage.publish import publish_file


def read_artifact_records(data_dir: Path | str, session_id: str) -> list[dict]:
    index_path = artifact_index_path(data_dir, session_id)
    if not index_path.exists():
        return []
    records: list[dict] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def append_artifact_records(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> Path:
    """Append artifact metadata to the session artifact index."""
    _publish_work_backed_artifacts(data_dir, session_id, artifacts)
    index_path = artifact_index_path(data_dir, session_id)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        _artifact_identity(record)
        for record in read_artifact_records(data_dir, session_id)
    }
    with index_path.open("a", encoding="utf-8") as handle:
        for artifact in artifacts:
            record = artifact.model_dump(mode="json")
            identity = _artifact_identity(record)
            if identity in existing:
                continue
            handle.write(json.dumps(record) + "\n")
            existing.add(identity)
    return index_path


def _artifact_identity(record: dict) -> str:
    """Stable identity for replay-safe artifact registration."""
    return json.dumps(
        {
            key: record.get(key)
            for key in (
                "artifact_id",
                "file_path",
                "file_hash",
                "file_size_bytes",
                "artifact_type",
                "stage",
                "method_name",
                "metadata",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _publish_work_backed_artifacts(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> None:
    """Give registered work products a durable artifact path in new sessions."""
    if not is_session_run_dir(data_dir, session_id):
        return
    run_dir = Path(data_dir)
    work_dir = run_dir / "work"
    for artifact in artifacts:
        source = Path(artifact.file_path)
        try:
            relative = source.relative_to(work_dir)
        except ValueError:
            continue
        destination = run_dir / "artifacts" / artifact.stage / session_id / "work_products" / relative
        published = publish_file(source, destination)
        artifact.file_path = published
