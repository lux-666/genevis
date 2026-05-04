"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

READ_QC_LONG_READ_STATS = "read_qc.long_read_stats"
READ_QC_FILTERED_LONG_READS = "read_qc.filtered_long_reads"
READ_QC_LONG_READS_FOFN = "read_qc.long_reads.fofn"
READ_QC_FILTERED_LONG_READS_FOFN = "read_qc.filtered_long_reads.fofn"


def file_sha256(path: Path) -> str:
    """Return a file SHA256 digest."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactMeta(BaseModel):
    """Metadata stored in state/ledger instead of large file contents."""

    artifact_id: str
    file_path: Path
    file_hash: str
    file_size_bytes: int
    artifact_type: str
    stage: str
    method_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        stage: str,
        method_name: str,
        artifact_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ArtifactMeta":
        """Build artifact metadata for an existing file."""
        return cls(
            artifact_id=f"{stage}:{method_name}:{path.name}",
            file_path=path,
            file_hash=file_sha256(path),
            file_size_bytes=path.stat().st_size,
            artifact_type=artifact_type,
            stage=stage,
            method_name=method_name,
            metadata=metadata or {},
        )


class GenomeSurveyResult(BaseModel):
    """Compact genome survey contract written by the genome_survey stage."""

    session_id: str
    mode: Literal["full", "fallback_low_confidence", "skipped"]
    status: Literal["planned", "skipped"]
    confidence: Literal["high", "low", "none"]
    input_asset_refs: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    estimates: dict[str, Any] = Field(default_factory=dict)
    skip_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyCandidateBundle(BaseModel):
    """Planned assembly candidate bundle without running an assembler."""

    session_id: str
    candidate_id: str
    method_name: str
    status: Literal["planned", "rejected"]
    artifact_type: str = "draft_assembly.candidate"
    input_ref: str
    input_type: str
    genome_size_prior: int | None = None
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    logs: dict[str, str] = Field(default_factory=dict)
    rejection_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyEvalSignal(BaseModel):
    """One evaluation signal for an assembly candidate.

    Thresholds are not stored here; they are human-approved policy
    applied during rule evaluation.
    """

    signal_name: str
    method_name: str
    candidate_id: str
    status: Literal["pending", "available", "missing", "failed"]
    metric_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyEvalBundle(BaseModel):
    """Collection of evaluation signals for one or more assembly candidates.

    Does not contain thresholds or decisions; only what was collected
    or referenced.
    """

    session_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    signals: list[AssemblyEvalSignal] = Field(default_factory=list)
    available_inputs: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewDecisionArtifact(BaseModel):
    """Human or deterministic review decision recorded as an artifact."""

    session_id: str
    stage: str
    decision: Literal[
        "accept",
        "rerun",
        "purge",
        "continue_low_confidence",
        "stop_human_review",
    ]
    candidate_id: str | None = None
    rejected_candidates: list[str] = Field(default_factory=list)
    final_assembly_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
