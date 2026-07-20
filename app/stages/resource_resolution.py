"""Canonical cross-stage resource resolution and update helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.resource_ref import ResourceRef
from app.schemas.state import ResourceRefs, TopLevelState, validate_resource_refs


@dataclass(frozen=True)
class ResourceSpec:
    """Simplified resource specification for unified resolution.

    Stage-specific output names deliberately do not belong in this contract.
    """

    resource_type: str
    """Logical resource name: 'long_reads', 'assembly', etc."""

    asset_types: list[str] = field(default_factory=list)
    """Asset types to search in asset registry as fallback."""

    asset_type_priority_groups: list[list[str]] = field(default_factory=list)
    """Ordered equivalent-type groups; the first present group wins."""

    required: bool = True
    """Whether callers/preflight must treat a missing result as blocking."""

    allow_multiple: bool = False
    """Whether to return all matches (True) or first match (False)."""


@dataclass(frozen=True)
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


def resolve_stage_resource(
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

    Stage checkpoint cursors are deliberately not searched.

    Args:
        state: TopLevelState containing resource_refs and asset_registry_ref
        resource_spec: Resource requirement specification
        override: Optional run_config override path

    Returns:
        ResourceResolution with paths, source, metadata

    Examples:
        >>> spec = ResourceSpec(
        ...     resource_type="long_reads",
        ...     asset_types=["raw_long_reads"],
        ... )
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
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

        paths = list(dict.fromkeys(resource_ref.paths))
        if resource_ref.source_stage in {None, "inventory"}:
            assets = resource_ref.metadata.get("assets") or []
            if isinstance(assets, list):
                preferred_assets = _preferred_assets(assets, resource_spec)
                preferred_paths = [
                    str(asset["path"])
                    for asset in preferred_assets
                    if asset.get("path")
                ]
                if preferred_paths:
                    paths = list(dict.fromkeys(preferred_paths))
        if paths:
            if not resource_spec.allow_multiple and len(paths) > 1:
                paths = paths[:1]
            return ResourceResolution(
                found=True,
                paths=paths,
                source=resource_ref.source_stage or "resource_refs",
                source_type="resource_refs",
                metadata=dict(resource_ref.metadata),
            )

    # 3. Fallback to asset registry
    if resource_spec.asset_types:
        from app.stages.base import load_assets_from_registry

        assets = load_assets_from_registry(state, resource_spec.asset_types)
        assets = _preferred_assets(assets, resource_spec)
        if assets:
            paths = [str(asset["path"]) for asset in assets if asset.get("path")]
            if paths:
                # Preserve enough registry provenance for downstream stages
                # without requiring them to reopen the registry.
                first_asset = assets[0]
                metadata = {
                    "platform": first_asset.get("platform"),
                    "format": first_asset.get("format"),
                    "asset_type": first_asset.get("asset_type"),
                    "asset_ids": [
                        str(asset["asset_id"])
                        for asset in assets
                        if asset.get("asset_id")
                    ],
                    "platforms": list(
                        dict.fromkeys(
                            str(asset["platform"])
                            for asset in assets
                            if asset.get("platform")
                        )
                    ),
                    "formats": list(
                        dict.fromkeys(
                            str(asset["format"])
                            for asset in assets
                            if asset.get("format")
                        )
                    ),
                    "asset_types": list(
                        dict.fromkeys(
                            str(asset["asset_type"])
                            for asset in assets
                            if asset.get("asset_type")
                        )
                    ),
                    "assets": [dict(asset) for asset in assets],
                }

                return ResourceResolution(
                    found=True,
                    paths=paths if resource_spec.allow_multiple else paths[:1],
                    source="asset_registry",
                    source_type="asset",
                    metadata=metadata,
                )

    # 4. Resource not found
    return ResourceResolution(
        found=False,
        paths=[],
        source="missing",
        source_type="missing",
    )


def _preferred_assets(
    assets: list[dict[str, Any]], resource_spec: ResourceSpec
) -> list[dict[str, Any]]:
    """Select the first available equivalent-type group in registry order."""

    groups = resource_spec.asset_type_priority_groups or [
        [asset_type] for asset_type in resource_spec.asset_types
    ]
    for group in groups:
        selected = [asset for asset in assets if asset.get("asset_type") in group]
        if selected:
            return selected
    return assets


def resolve_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
    override: str | None = None,
) -> ResourceResolution:
    """Compatibility name for the canonical resolver."""

    return resolve_stage_resource(
        state,
        resource_spec=resource_spec,
        override=override,
    )


def update_resource_ref(
    state: TopLevelState,
    resource_type: str,
    paths: list[str],
    source_stage: str,
    metadata: dict[str, Any] | None = None,
) -> ResourceRefs:
    """
    Return a validated resource-table replacement containing the new reference.

    This should be called by stage commit functions to register their outputs.
    The input state is never mutated.

    Args:
        state: TopLevelState to update
        resource_type: Logical resource name
        paths: File paths for this resource
        source_stage: Stage name that produced this resource
        metadata: Optional metadata (size, count, quality, etc.)

    Example:
        >>> # In assembly stage commit
        >>> replacement = update_resource_ref(
        ...     state,
        ...     resource_type="assembly",
        ...     paths=["/data/assembly.fa"],
        ...     source_stage="assembly",
        ...     metadata={"contig_count": 1523, "n50": 45000}
        ... )
    """
    resource_ref = ResourceRef(
        resource_type=resource_type,
        paths=list(dict.fromkeys(str(path) for path in paths if str(path).strip())),
        source_stage=source_stage,
        metadata=metadata or {},
    )
    refs = dict(state.get("resource_refs") or {})
    refs[resource_type] = resource_ref.to_dict()
    return validate_resource_refs(refs)
