"""Shared utilities for stage nodes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import (
    DOMAIN_STAGES,
    DomainStage,
    JsonValue,
    StageCursor,
    TopLevelState,
    validate_stage_outputs,
)
from app.storage.paths import SessionPaths


def ordered_unique(values: list[str]) -> list[str]:
    """Return strings in first-seen order with duplicates removed."""

    return list(dict.fromkeys(values))


def absolute_data_dir(state: TopLevelState) -> Path:
    """Return data_dir as a resolved absolute path.

    The runner sets cwd to the attempt working directory, which is nested
    under data_dir. A relative data_dir interpolated into a generated shell
    script therefore resolves from inside that workdir and points at a
    non-existent nested location. Resolve the root once before scripts are
    generated so local and cloud runs use stable absolute paths.
    """
    return Path(state.get("data_dir", "data")).expanduser().resolve(strict=False)


def session_paths(state: TopLevelState) -> SessionPaths:
    """Return the validated v2 path owner for the current run.

    Runtime state carries the run root, not the shared ``data/`` root.  Keeping
    this conversion here prevents stage code from reconstructing storage roots
    or appending the session identifier below an already session-scoped path.
    """

    paths = SessionPaths.from_run_dir(absolute_data_dir(state))
    paths.require_session_id(state["session_id"])
    return paths


def stage_artifacts_dir(state: TopLevelState, stage: str, *parts: str) -> Path:
    """Return a path below ``artifacts/<stage>/`` for this run."""

    return session_paths(state).artifact_path(stage, *parts)


def stage_work_dir(state: TopLevelState, stage: str, *parts: str) -> Path:
    """Return a path below ``work/<stage>/`` for this run."""

    return session_paths(state).work_path(stage, *parts)


def resolve_run_ref(state: TopLevelState, path_ref: str | Path) -> Path:
    """Resolve a durable run-relative ref without accepting path traversal."""

    path = Path(path_ref)
    if path.is_absolute():
        # External biological inputs remain absolute execution dependencies;
        # internal durable refs are always run-relative and take this branch.
        return path.resolve(strict=False)
    return session_paths(state).resolve_ref(path.as_posix())


# ============================================================================
# Stage output read/write helpers
# ============================================================================


def stage_output(state: TopLevelState, stage_name: str) -> StageCursor:
    """Return a mutable copy of a stage's slot in stage_outputs."""
    if stage_name not in DOMAIN_STAGES:
        raise ValueError(f"unknown stage output slot: {stage_name}")
    stage = cast(DomainStage, stage_name)
    return dict((state.get("stage_outputs") or {}).get(stage, {}))


def update_stage_output(
    state: TopLevelState, stage_name: str, payload: dict[str, JsonValue]
) -> dict[str, object]:
    """Merge payload into a stage's output slot and return the state delta."""
    outputs = dict(state.get("stage_outputs") or {})
    outputs[cast(DomainStage, stage_name)] = {
        **stage_output(state, stage_name),
        **payload,
    }
    return {"stage_outputs": validate_stage_outputs(outputs)}


def append_route_trace(state: TopLevelState, *items: str) -> list[str]:
    """Append route items while retaining only the newest bounded history."""

    return [*list(state.get("route_trace") or []), *items][-ROUTE_TRACE_LIMIT:]


# ============================================================================
# Universal Resource Resolution System
# ============================================================================


@dataclass
class ResourceSpec:
    """Specification for resolving a resource.

    Defines what resource to find and where to look for it.
    """

    resource_type: str
    """Type identifier: 'assembly', 'reads', 'clean_reads', 'masked_assembly', etc."""

    prefer_stages: list[str] = field(default_factory=list)
    """Stage names to check first, in priority order."""

    output_keys: dict[str, list[str]] = field(default_factory=dict)
    """Output keys to search in each stage: {stage_name: [key1, key2]}"""

    asset_types: list[str] = field(default_factory=list)
    """Asset types to search in asset registry as fallback."""

    required: bool = True
    """Whether this resource is required (fail if missing) or optional."""

    allow_multiple: bool = False
    """Whether to return all matches (True) or first match (False)."""


@dataclass
class ResourceResolution:
    """Result of resource resolution."""

    found: bool
    """Whether the resource was found."""

    paths: list[str]
    """Resolved paths (multiple if allow_multiple=True)."""

    source: str
    """Source identifier: 'run_config', '<stage_name>', 'asset_registry', or 'missing'."""

    source_type: str
    """Source type: 'override', 'stage_output', 'asset', or 'missing'."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata (platform, format, etc.)."""

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
    Universal resource resolver for all stage inputs.

    Resolution order:
    1. RunConfig override (if provided)
    2. Preferred stages outputs (check unified + legacy keys)
    3. All stage outputs in reverse STAGE_ORDER (auto-fallback)
    4. Asset registry (specified asset_types)

    Args:
        state: TopLevelState containing stage_outputs and asset_registry_ref
        resource_spec: Resource requirement specification
        override: Optional run_config override path

    Returns:
        ResourceResolution with paths, source, metadata

    Examples:
        # Example 1: Using preferred stages
        >>> spec = ResourceSpec(
        ...     resource_type="assembly",
        ...     prefer_stages=["hic_scaffolding", "assembly_review"],
        ...     output_keys={
        ...         "hic_scaffolding": ["final_assembly_for_annotation"],
        ...         "assembly_review": ["final_assembly_ref"],
        ...     },
        ...     asset_types=["assembly_fasta_registered"],
        ... )
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "hic_scaffolding" (if it ran)

        # Example 2: Auto-fallback to non-preferred stage
        >>> # Assume only assembly ran (not in prefer_stages)
        >>> state.stage_outputs = {
        ...     "assembly": {
        ...         "draft_assembly_ref": "data/draft.fa"
        ...     }
        ... }
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "assembly" (auto-fallback)
        >>> print(resolution.source_type)  # "stage_output"

        # Example 3: Fallback to asset registry
        >>> # No stage outputs available
        >>> state.stage_outputs = {}
        >>> # But manifest has registered assembly
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "asset_registry"
        >>> print(resolution.source_type)  # "asset"

        # Example 4: RunConfig override skips all auto-resolution
        >>> resolution = resolve_stage_resource(
        ...     state,
        ...     resource_spec=spec,
        ...     override="/custom/path/assembly.fa"
        ... )
        >>> print(resolution.source)  # "run_config"
        >>> print(resolution.path)  # "/custom/path/assembly.fa"
    """
    # 1. RunConfig override
    if override:
        return ResourceResolution(
            found=True,
            paths=[str(override)],
            source="run_config",
            source_type="override",
        )

    outputs = cast(
        Mapping[str, Mapping[str, JsonValue]],
        state.get("stage_outputs") or {},
    )

    # 2. Check preferred stages
    if resource_spec.prefer_stages:
        for stage in resource_spec.prefer_stages:
            resolution = _check_stage_output(stage, outputs, resource_spec)
            if resolution:
                return resolution

    # 3. Auto-fallback: check all stages in reverse STAGE_ORDER
    from app.workflow import STAGE_ORDER

    for stage in reversed(STAGE_ORDER):
        if stage in resource_spec.prefer_stages:
            continue  # Already checked
        resolution = _check_stage_output(stage, outputs, resource_spec)
        if resolution:
            return resolution

    # 4. Fallback to asset registry
    if resource_spec.asset_types:
        resolution = _check_asset_registry(state, resource_spec)
        if resolution:
            return resolution

    # Not found
    return ResourceResolution(
        found=False,
        paths=[],
        source="missing",
        source_type="missing",
    )


def _check_stage_output(
    stage: str,
    outputs: Mapping[str, Mapping[str, JsonValue]],
    spec: ResourceSpec,
) -> ResourceResolution | None:
    """Check if a stage output contains the resource."""
    stage_out = outputs.get(stage) or {}

    # First check unified key (final_*_for_downstream)
    unified_key = f"final_{spec.resource_type}_for_downstream"
    if stage_out.get(unified_key):
        return ResourceResolution(
            found=True,
            paths=[str(stage_out[unified_key])],
            source=stage,
            source_type="stage_output",
        )

    # Then check stage-specific keys
    keys_to_check = spec.output_keys.get(stage, [])
    for key in keys_to_check:
        if stage_out.get(key):
            value = stage_out[key]
            # Handle both single value and list
            if isinstance(value, list):
                paths = [str(v) for v in value if v]
            else:
                paths = [str(value)]

            if paths:
                return ResourceResolution(
                    found=True,
                    paths=paths if spec.allow_multiple else paths[:1],
                    source=stage,
                    source_type="stage_output",
                )

    return None


def _check_asset_registry(
    state: TopLevelState,
    spec: ResourceSpec,
) -> ResourceResolution | None:
    """Check if asset registry contains the resource."""
    assets = load_assets_from_registry(state, spec.asset_types)
    if not assets:
        return None

    paths = [str(asset["path"]) for asset in assets if asset.get("path")]
    if not paths:
        return None

    # Extract metadata from first asset
    first_asset = assets[0]
    metadata = {
        "platform": first_asset.get("platform"),
        "format": first_asset.get("format"),
        "asset_type": first_asset.get("asset_type"),
    }

    return ResourceResolution(
        found=True,
        paths=paths if spec.allow_multiple else paths[:1],
        source="asset_registry",
        source_type="asset",
        metadata=metadata,
    )


def load_assets_from_registry(
    state: TopLevelState,
    asset_types: list[str],
) -> list[dict[str, Any]]:
    """
    Load assets from registry by type.

    Helper for reducing asset registry query boilerplate.
    Handles JSON parsing errors gracefully.

    Args:
        state: TopLevelState
        asset_types: List of asset_type values to match

    Returns:
        List of matching assets, or empty list if not found/error
    """
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []

    registry_path = Path(str(registry_ref))
    if not registry_path.exists():
        return []

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assets = registry.get("assets", [])
        return [asset for asset in assets if asset.get("asset_type") in asset_types]
    except (OSError, json.JSONDecodeError):
        return []
