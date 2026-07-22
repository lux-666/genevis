"""Shared lifecycle helpers for executable genome-survey methods."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.storage.artifacts import publish_or_recover_work_artifact
from app.storage.ledger import Ledger


def genome_survey_output(state: TopLevelState) -> dict[str, Any]:
    """Return a shallow copy of the genome-survey stage output."""
    return dict((state.get("stage_outputs") or {}).get("genome_survey", {}))


def update_genome_survey_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    """Merge a method update into the genome-survey stage output."""
    outputs = dict(state.get("stage_outputs") or {})
    outputs["genome_survey"] = {**genome_survey_output(state), **payload}
    return {"stage_outputs": outputs}


def attempt_artifacts(
    state: TopLevelState,
    attempt: FrozenAttemptSpec,
    artifact_types_by_path: dict[str, str],
) -> list[ArtifactMeta]:
    """Publish or recover expected work outputs and return their metadata."""
    artifacts: list[ArtifactMeta] = []
    for path in attempt.expected_outputs:
        artifact_type = artifact_types_by_path[str(path)]
        metadata = {"attempt_id": attempt.attempt_id}
        published = publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            path,
            stage="genome_survey",
            method_name=attempt.method_name,
            artifact_type=artifact_type,
            metadata=metadata,
        )
        artifacts.append(
            ArtifactMeta.from_file(
                published.path,
                stage="genome_survey",
                method_name=attempt.method_name,
                artifact_type=artifact_type,
                metadata=metadata,
            )
        )
    return artifacts


def record_attempt_success(
    state: TopLevelState,
    attempt: FrozenAttemptSpec,
    artifacts: list[ArtifactMeta],
) -> None:
    """Register method artifacts and append successful-attempt ledger events."""
    data_dir = Path(state.get("data_dir", "data"))
    ledger = Ledger(data_dir)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(
            attempt.session_id,
            artifact,
            attempt_id=attempt.attempt_id,
        )
