"""Snapshot operator-supplied Hi-C results without judging their structure."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.control.errors import ResumeValidationError
from app.review.application import ForegroundReviewMetadataV1
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import (
    HIC_CONTACT_MAP,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
)
from app.schemas.state import TopLevelState
from app.stages.base import session_paths
from app.stages.sequences import validate_assembly_fasta
from app.storage.artifacts import publish_artifact_record, resolve_artifact_ref

MANUAL_ASSEMBLY_METHOD = "hic_scaffolding.manual_assembly"


class HiCAdoptionMetadataV1(ForegroundReviewMetadataV1):
    assembly_fasta: str | None = None
    contact_map: str | None = None
    assembly_fasta_ref: ArtifactRef | None = None
    contact_map_ref: ArtifactRef | None = None


def snapshot_manual_assembly_inputs(
    state: TopLevelState,
    request: ResumeReviewRequest,
    record: Mapping[str, Any],
) -> ResumeReviewRequest:
    """Replace submitted paths with immutable refs before committing intent."""
    metadata = HiCAdoptionMetadataV1.model_validate(request.metadata)
    if request.decision != "use_manual_assembly":
        if any(
            (
                metadata.assembly_fasta,
                metadata.contact_map,
                metadata.assembly_fasta_ref,
                metadata.contact_map_ref,
            )
        ):
            raise ResumeValidationError("manual files require use_manual_assembly")
        return request
    if request.decided_by != "human":
        raise ResumeValidationError("manual assembly requires a human submission")
    if request.decision not in record["payload"]["decisions_allowed"]:
        raise ResumeValidationError("manual assembly is not allowed by this request")
    if not metadata.assembly_fasta and metadata.assembly_fasta_ref is None:
        raise ResumeValidationError("use_manual_assembly requires assembly_fasta")
    paths = session_paths(state)
    step_id = str(record["workflow_step_id"])
    refs: dict[str, Any] = metadata.model_dump(mode="json", exclude_unset=True)
    try:
        for field, artifact_type in (
            ("assembly_fasta", HIC_SCAFFOLDED_ASSEMBLY),
            ("contact_map", HIC_CONTACT_MAP),
        ):
            source_path = getattr(metadata, field)
            ref = getattr(metadata, f"{field}_ref")
            if source_path and ref is not None:
                raise ValueError(f"provide either {field} or {field}_ref")
            if source_path:
                source = Path(source_path).expanduser()
                work = paths.workflow_step_work_path(
                    step_id, "hic_scaffolding", "manual", uuid4().hex
                )
                work.mkdir(parents=True, exist_ok=True)
                temporary = work / field
                try:
                    shutil.copyfile(source, temporary)
                    if field == "assembly_fasta":
                        validate_assembly_fasta(temporary)
                    artifact = ArtifactMeta.from_file(
                        temporary,
                        stage="hic_scaffolding",
                        method_name=MANUAL_ASSEMBLY_METHOD,
                        artifact_type=artifact_type,
                    )
                    destination = paths.workflow_step_artifact_path(
                        step_id,
                        "hic_scaffolding",
                        "manual",
                        f"{field}.{artifact.file_hash}",
                    )
                    ref = publish_artifact_record(
                        paths.run_dir,
                        state["session_id"],
                        artifact,
                        destination=destination,
                    ).ref
                finally:
                    temporary.unlink(missing_ok=True)
                    work.rmdir()
            elif ref is not None:
                if ref.artifact_type != artifact_type:
                    raise ValueError(f"invalid {field}_ref artifact type")
                resolve_artifact_ref(paths.run_dir, state["session_id"], ref)
            refs.pop(field, None)
            if ref is not None:
                refs[f"{field}_ref"] = ref.model_dump(mode="json")
    except (OSError, ValueError) as exc:
        raise ResumeValidationError(f"manual assembly input is invalid: {exc}") from exc
    return request.model_copy(update={"metadata": refs})
