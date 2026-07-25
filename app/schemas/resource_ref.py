"""Resource reference schema for central resource table.

ResourceRef is the canonical cross-stage resource handoff protocol.
Replaces scattered stage_outputs key scanning with explicit resource declarations.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
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

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, str):
            raise TypeError("resource_type must be a string")
        if not self.resource_type.strip():
            raise ValueError("resource_type must not be empty")
        if not isinstance(self.paths, list):
            raise TypeError("paths must be a list")
        if not self.paths or not all(
            isinstance(path, str) and path.strip() for path in self.paths
        ):
            raise ValueError("paths must contain at least one non-empty string")
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dictionary")

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for state storage."""
        return {
            "resource_type": self.resource_type,
            "paths": list(self.paths),
            "source_stage": self.source_stage,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceRef":
        """Reconstruct from JSON dict."""
        unknown = set(data).difference(
            {"resource_type", "paths", "source_stage", "metadata"}
        )
        if unknown:
            raise ValueError(
                f"ResourceRef contains unknown fields: {', '.join(sorted(unknown))}"
            )
        if "resource_type" not in data:
            raise ValueError("ResourceRef.resource_type is required")
        paths = data.get("paths")
        if not isinstance(paths, list):
            raise TypeError("ResourceRef.paths must be a list")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("ResourceRef.metadata must be a dictionary")
        return cls(
            resource_type=str(data["resource_type"]),
            paths=list(paths),
            source_stage=(
                str(data["source_stage"])
                if data.get("source_stage") is not None
                else None
            ),
            metadata=dict(metadata),
        )


# Logical resource name definitions
LOGICAL_RESOURCES = {
    "long_reads": "Long-read sequencing data (ONT, PacBio)",
    "short_reads": "Short-read sequencing data (Illumina)",
    "assembly": "Assembly result (updated by dedup/review/hic/t2t)",
    "masked_assembly": "Repeat-masked assembly",
    "predicted_proteins": "Predicted protein sequences",
    "hic_reads": "Hi-C sequencing data",
}
