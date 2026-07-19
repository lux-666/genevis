"""Unified resource resolution using resource_refs.

This module provides the new resource resolution mechanism that uses
the central resource_refs table, with fallback to legacy mechanisms
during migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.resource_ref import ResourceRef
from app.schemas.state import TopLevelState


@dataclass
class ResourceSpec:
    """Simplified resource specification for unified resolution.

    Compared to the legacy ResourceSpec in base.py, this removes:
    - prefer_stages (replaced by resource_refs lookup)
    - output_keys (replaced by resource_refs lookup)
    """

    resource_type: str
    """Logical resource name: 'long_reads', 'assembly', etc."""

    asset_types: list[str] = field(default_factory=list)
    """Asset types to search in asset registry as fallback."""

    required: bool = True
    """Whether this resource is required (fail if missing) or optional."""

    allow_multiple: bool = False
    """Whether to return all matches (True) or first match (False)."""


@dataclass
class ResourceResolution:
    """Result of unified resource resolution."""

    found: bool
    """Whether the resource was found."""

    paths: list[str]
    """Resolved paths (multiple if allow_multiple=True)."""

    source: str
    """Source identifier: 'override', 'resource_refs', 'asset_registry', or 'missing'."""

    source_type: str
    """Source type: 'override', 'resource_refs', 'asset', or 'missing'."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata from ResourceRef or asset."""

    @property
    def path(self) -> str | None:
        """Convenience property for single-path access."""
        return self.paths[0] if self.paths else None


class ResourceNotFoundError(Exception):
    """Required resource was not found."""

    def __init__(self, resource_type: str, message: str | None = None):
        self.resource_type = resource_type
        msg = message or f"Required resource '{resource_type}' not found"
        super().__init__(msg)


def resolve_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
    override: str | None = None,
) -> ResourceResolution:
    """
    Unified resource resolver using resource_refs.

    Resolution order:
    1. RunConfig override (if provided)
    2. state.resource_refs[resource_type] (central resource table)
    3. Asset registry (specified asset_types)

    This replaces the legacy prefer_stages and output_keys scanning.

    Args:
        state: TopLevelState containing resource_refs and asset_registry_ref
        resource_spec: Resource requirement specification
        override: Optional run_config override path

    Returns:
        ResourceResolution with paths, source, metadata

    Raises:
        ResourceNotFoundError: If required resource not found

    Examples:
        >>> spec = ResourceSpec(
        ...     resource_type="long_reads",
        ...     asset_types=["raw_long_reads"],
        ... )
        >>> resolution = resolve_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "resource_refs" or "asset_registry"
        >>> print(resolution.paths)   # ["/data/reads.fastq.gz"]
    """
    # 1. RunConfig override
    if override:
        return ResourceResolution(
            found=True,
            paths=[str(override)],
            source="override",
            source_type="override",
        )

    # 2. Central resource_refs table
    resource_refs = state.get("resource_refs", {})
    if resource_spec.resource_type in resource_refs:
        ref_data = resource_refs[resource_spec.resource_type]

        # Handle both ResourceRef objects and dicts (for serialization)
        if isinstance(ref_data, dict):
            resource_ref = ResourceRef.from_dict(ref_data)
        else:
            resource_ref = ref_data

        paths = resource_ref.paths
        if not resource_spec.allow_multiple and len(paths) > 1:
            paths = paths[:1]

        return ResourceResolution(
            found=True,
            paths=paths,
            source=resource_ref.source_stage or "resource_refs",
            source_type="resource_refs",
            metadata=resource_ref.metadata,
        )

    # 3. Fallback to asset registry
    if resource_spec.asset_types:
        from app.stages.base import load_assets_from_registry

        assets = load_assets_from_registry(state, resource_spec.asset_types)
        if assets:
            paths = [str(asset["path"]) for asset in assets if asset.get("path")]
            if paths:
                # Extract metadata from first asset
                first_asset = assets[0]
                metadata = {
                    "platform": first_asset.get("platform"),
                    "format": first_asset.get("format"),
                    "asset_type": first_asset.get("asset_type"),
                }

                return ResourceResolution(
                    found=True,
                    paths=paths if resource_spec.allow_multiple else paths[:1],
                    source="asset_registry",
                    source_type="asset",
                    metadata=metadata,
                )

    # 4. Resource not found
    if resource_spec.required:
        raise ResourceNotFoundError(
            resource_spec.resource_type,
            f"Required resource '{resource_spec.resource_type}' not found. "
            f"Checked: resource_refs, asset_types={resource_spec.asset_types}",
        )

    return ResourceResolution(
        found=False,
        paths=[],
        source="missing",
        source_type="missing",
    )


def update_resource_ref(
    state: TopLevelState,
    resource_type: str,
    paths: list[str],
    source_stage: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Update or create a resource reference in the central table.

    This should be called by stage commit functions to register their outputs.

    Args:
        state: TopLevelState to update
        resource_type: Logical resource name
        paths: File paths for this resource
        source_stage: Stage name that produced this resource
        metadata: Optional metadata (size, count, quality, etc.)

    Example:
        >>> # In assembly stage commit
        >>> update_resource_ref(
        ...     state,
        ...     resource_type="assembly",
        ...     paths=["/data/assembly.fa"],
        ...     source_stage="assembly",
        ...     metadata={"contig_count": 1523, "n50": 45000}
        ... )
    """
    if "resource_refs" not in state:
        state["resource_refs"] = {}

    resource_ref = ResourceRef(
        resource_type=resource_type,
        paths=paths,
        source_stage=source_stage,
        metadata=metadata or {},
    )

    state["resource_refs"][resource_type] = resource_ref.to_dict()
