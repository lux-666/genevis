"""Shared lifecycle helpers for executable genome-survey methods."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.genome_survey.state import (
    GenomeSurveyCursor,
    genome_survey_output,
    update_genome_survey_output,
)
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    record_registered_artifacts,
)
from app.storage.ledger import Ledger


def survey_output(state: TopLevelState) -> GenomeSurveyCursor:
    """Return a shallow copy of the genome-survey stage output."""
    return genome_survey_output(state)


def update_survey_output(
    state: TopLevelState, payload: Mapping[str, object]
) -> StateUpdate:
    """Merge a method update into the genome-survey stage output."""
    return update_genome_survey_output(state, payload)


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
    ledger.record_attempt_succeeded(
        attempt.session_id, attempt, artifacts=artifact_payloads
    )
    record_registered_artifacts(
        data_dir,
        attempt.session_id,
        artifact_payloads,
        attempt_id=attempt.attempt_id,
    )
