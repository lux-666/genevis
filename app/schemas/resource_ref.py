"""Resource reference schema for central resource table.

ResourceRef is the canonical cross-stage resource handoff protocol.
Replaces scattered stage_outputs key scanning with explicit resource declarations.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceRef:
    """Central resource reference for cross-stage resource handoff.

    This is the single source of truth for resource locations across stages.
    Resources are identified by logical names (e.g., 'long_reads', 'assembly')
    rather than stage-specific output keys.

    Attributes:
        resource_type: Logical resource name ('long_reads', 'assembly', etc.)
        paths: One or more file paths for this resource
        source_stage: Which stage produced this resource (None for manifest)
        metadata: Optional metadata (size, count, quality metrics, etc.)
    """

    resource_type: str
    paths: list[str]
    source_stage: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for state storage."""
        return {
            "resource_type": self.resource_type,
            "paths": self.paths,
            "source_stage": self.source_stage,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceRef":
        """Reconstruct from JSON dict."""
        return cls(
            resource_type=data["resource_type"],
            paths=data["paths"],
            source_stage=data.get("source_stage"),
            metadata=data.get("metadata", {}),
        )


# Logical resource name definitions
LOGICAL_RESOURCES = {
    "long_reads": "Long-read sequencing data (ONT, PacBio)",
    "short_reads": "Short-read sequencing data (Illumina)",
    "assembly": "Assembly result (updated by dedup/review/hic/t2t)",
    "masked_assembly": "Repeat-masked assembly",
    "predicted_proteins": "Predicted protein sequences",
    "hic_reads": "Hi-C sequencing data",
    "genome_size_estimate": "Genome size estimate from survey",
}
