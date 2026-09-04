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
        workflow_step_id: Frozen occurrence that produced this resource
        metadata: Optional metadata (size, count, quality metrics, etc.)
    """

    resource_type: str
    paths: list[str]
    source_stage: str | None = None
    workflow_step_id: str | None = None
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
        if self.workflow_step_id is not None and (
            not isinstance(self.workflow_step_id, str)
            or not self.workflow_step_id.strip()
        ):
            raise ValueError("workflow_step_id must be a non-empty string or None")
        if self.source_stage is not None and (
            not isinstance(self.source_stage, str) or not self.source_stage.strip()
        ):
            raise ValueError("source_stage must be a non-empty string or None")
        if (self.source_stage is None) != (self.workflow_step_id is None):
            raise ValueError(
                "source_stage and workflow_step_id must either both be set "
                "or both be None"
            )
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dictionary")

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for state storage."""
        return {
            "resource_type": self.resource_type,
            "paths": list(self.paths),
            "source_stage": self.source_stage,
            "workflow_step_id": self.workflow_step_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceRef":
        """Reconstruct from JSON dict."""
        unknown = set(data).difference(
            {
                "resource_type",
                "paths",
                "source_stage",
                "workflow_step_id",
                "metadata",
            }
        )
        if unknown:
            raise ValueError(
                f"ResourceRef contains unknown fields: {', '.join(sorted(unknown))}"
            )
        if "resource_type" not in data:
            raise ValueError("ResourceRef.resource_type is required")
        resource_type = data["resource_type"]
        if not isinstance(resource_type, str):
            raise TypeError("ResourceRef.resource_type must be a string")
        source_stage = data.get("source_stage")
        if source_stage is not None and not isinstance(source_stage, str):
            raise TypeError("ResourceRef.source_stage must be a string or None")
        workflow_step_id = data.get("workflow_step_id")
        if workflow_step_id is not None and not isinstance(workflow_step_id, str):
            raise TypeError("ResourceRef.workflow_step_id must be a string or None")
        paths = data.get("paths")
        if not isinstance(paths, list):
            raise TypeError("ResourceRef.paths must be a list")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("ResourceRef.metadata must be a dictionary")
        return cls(
            resource_type=resource_type,
            paths=list(paths),
            source_stage=source_stage,
            workflow_step_id=workflow_step_id,
            metadata=dict(metadata),
        )


# Logical resource name definitions
LOGICAL_RESOURCES = {
    "long_reads": "Long-read sequencing data (ONT, PacBio)",
    "short_reads": "Short-read sequencing data (Illumina)",
    "assembly": "Assembly result (updated by dedup/review/hic/t2t)",
    "hic_reads": "Hi-C sequencing data",
    "reference_genome": "Per-run reference genome FASTA for reference-based evaluation",
}
