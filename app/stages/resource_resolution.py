"""Canonical cross-stage resource resolution and update helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.control.workflow_plan import current_workflow_step_id
from app.schemas.artifacts import ArtifactRef
from app.schemas.assets import AssetRecord
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
    """Source stage, ``resource_refs``, ``asset_registry``, or ``missing``."""

    source_type: str
    """Source type: ``resource_refs``, ``asset``, or ``missing``."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata from ResourceRef or asset."""

    asset_records: tuple[AssetRecord, ...] = ()
    """Verified runtime registry members, never serialized into state."""

    @property
    def path(self) -> str | None:
        """Convenience property for single-path access."""
        return self.paths[0] if self.paths else None


def resolve_stage_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
) -> ResourceResolution:
    """
    Unified resource resolver using resource_refs.

    Resolution order:
    1. state.resource_refs[resource_type] (central resource table)
    2. Asset registry (specified asset_types)

    Stage checkpoint cursors are deliberately not searched.

    Args:
        state: TopLevelState containing resource_refs and asset_registry_ref
        resource_spec: Resource requirement specification

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
    # 1. Central resource_refs table
    resource_refs = state.get("resource_refs", {})
    if resource_spec.resource_type in resource_refs:
        ref_data = resource_refs[resource_spec.resource_type]

        # Handle both ResourceRef objects and dicts (for serialization)
        if isinstance(ref_data, dict):
            resource_ref = ResourceRef.from_dict(ref_data)
        else:
            resource_ref = ref_data

        paths = list(dict.fromkeys(resource_ref.paths))
        runtime_metadata = dict(resource_ref.metadata)
        selected_records: list[AssetRecord] = []
        has_registry_ref = "asset_registry_ref" in resource_ref.metadata
        has_asset_ids = "asset_ids" in resource_ref.metadata
        if has_registry_ref != has_asset_ids:
            raise ValueError(
                f"registry-backed resource {resource_spec.resource_type!r} "
                "must carry both asset_registry_ref and asset_ids"
            )
        if has_registry_ref:
            resource_registry_ref = ArtifactRef.model_validate(
                resource_ref.metadata.get("asset_registry_ref")
            )
            current_registry_ref = ArtifactRef.model_validate(
                state.get("asset_registry_ref")
            )
            if resource_registry_ref != current_registry_ref:
                raise ValueError(
                    f"registry-backed resource {resource_spec.resource_type!r} "
                    "does not reference the current asset registry"
                )
            asset_ids = list(resource_ref.metadata.get("asset_ids") or [])
            if not asset_ids:
                raise ValueError(
                    f"registry-backed resource {resource_spec.resource_type!r} "
                    "has no authoritative asset IDs"
                )
            from app.storage.asset_registry import records_by_ordered_ids

            records = records_by_ordered_ids(
                state,
                [str(asset_id) for asset_id in asset_ids],
                expected_paths=resource_ref.paths,
            )
            selected_records = _preferred_records(records, resource_spec)
            if not resource_spec.allow_multiple:
                selected_records = selected_records[:1]
            paths = [str(record.path) for record in selected_records]
            runtime_metadata = _metadata_from_records(
                runtime_metadata,
                selected_records,
            )
        if paths:
            if not resource_spec.allow_multiple and len(paths) > 1:
                paths = paths[:1]
            return ResourceResolution(
                found=True,
                paths=paths,
                source=resource_ref.source_stage or "resource_refs",
                source_type="resource_refs",
                metadata=runtime_metadata,
                asset_records=tuple(selected_records),
            )

    # 2. Fallback to asset registry
    if resource_spec.asset_types:
        from app.storage.asset_registry import load_asset_records

        records = load_asset_records(
            state,
            set(resource_spec.asset_types),
        )
        records = _preferred_records(records, resource_spec)
        if records:
            if not resource_spec.allow_multiple:
                records = records[:1]
            paths = [str(record.path) for record in records]
            if paths:
                return ResourceResolution(
                    found=True,
                    paths=paths,
                    source="asset_registry",
                    source_type="asset",
                    metadata=_metadata_from_records({}, records),
                    asset_records=tuple(records),
                )

    # 3. Resource not found
    return ResourceResolution(
        found=False,
        paths=[],
        source="missing",
        source_type="missing",
    )


def _preferred_records(
    records: list[AssetRecord],
    resource_spec: ResourceSpec,
) -> list[AssetRecord]:
    """Apply lifecycle priority without collapsing distinct read platforms."""

    # Trio parent files are deliberately registered so the Trio adapter can
    # resolve their explicit role refs.  They are not offspring genomic
    # resources, however, and must never be recovered by the generic central
    # long/short-read fallback used by Survey or Read QC.
    if resource_spec.resource_type in {"long_reads", "short_reads"}:
        records = [
            record
            for record in records
            if record.metadata.get("intake_role")
            not in {"trio_maternal_reads", "trio_paternal_reads"}
        ]

    groups = resource_spec.asset_type_priority_groups or [
        [asset_type] for asset_type in resource_spec.asset_types
    ]
    if not groups:
        return records
    if resource_spec.resource_type == "long_reads":
        selected_ids: set[str] = set()
        platforms = list(
            dict.fromkeys(str(record.platform or "unknown") for record in records)
        )
        for platform in platforms:
            platform_records = [
                record
                for record in records
                if str(record.platform or "unknown") == platform
            ]
            for group in groups:
                selected = [
                    record for record in platform_records if record.asset_type in group
                ]
                if selected:
                    selected_ids.update(record.asset_id for record in selected)
                    break
        return [record for record in records if record.asset_id in selected_ids]
    for group in groups:
        selected = [record for record in records if record.asset_type in group]
        if selected:
            return selected
    return records


def _metadata_from_records(
    base: dict[str, Any],
    records: list[AssetRecord],
) -> dict[str, Any]:
    """Build an invocation-local projection from authoritative typed records."""

    metadata = dict(base)
    first = records[0] if records else None
    metadata.update(
        {
            "platform": first.platform if first is not None else None,
            "format": first.format if first is not None else None,
            "compression": (
                str(first.metadata.get("compression") or "unknown")
                if first is not None
                else None
            ),
            "asset_type": first.asset_type if first is not None else None,
            "asset_ids": [record.asset_id for record in records],
            "compressions": [
                str(record.metadata.get("compression") or "unknown")
                for record in records
            ],
            "platforms": list(
                dict.fromkeys(
                    str(record.platform) for record in records if record.platform
                )
            ),
            "formats": list(
                dict.fromkeys(str(record.format) for record in records if record.format)
            ),
            "asset_types": list(dict.fromkeys(record.asset_type for record in records)),
            "assets": [record.model_dump(mode="json") for record in records],
        }
    )
    return metadata


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
        workflow_step_id=current_workflow_step_id(
            state,
            expected_stage=source_stage,
        ),
        metadata=metadata or {},
    )
    refs = dict(state.get("resource_refs") or {})
    refs[resource_type] = resource_ref.to_dict()
    return validate_resource_refs(refs)
