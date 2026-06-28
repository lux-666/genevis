"""Shared path helpers for stage subgraphs."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.state import TopLevelState


def absolute_data_dir(state: TopLevelState) -> Path:
    """Return data_dir as a resolved absolute path.

    The runner sets cwd to the attempt working directory, which is nested
    under data_dir. A relative data_dir interpolated into a generated shell
    script therefore resolves from inside that workdir and points at a
    non-existent nested location. Resolve the root once before scripts are
    generated so local and cloud runs use stable absolute paths.
    """
    return Path(state.get("data_dir", "data")).expanduser().resolve(strict=False)


def resolve_current_assembly(
    state: TopLevelState,
    *,
    override: str | None = None,
    prefer_stages: list[str] | None = None,
) -> tuple[str | None, str]:
    """Resolve the current best assembly from state.

    This resolver supports the three-layer control architecture:
    1. TUI/Plan layer: requested_stages controls which stages run
    2. RunConfig layer: override parameter allows stage-specific config
    3. Stage internal: each stage can specify prefer_stages for its needs

    Resolution order:
    1. RunConfig override (if provided) - Layer 2 control
    2. Preferred stages outputs (if specified, in order) - Layer 3 control
    3. All stage outputs in reverse STAGE_ORDER (automatic fallback)
    4. Asset registry (assembly_fasta_registered) - supports Layer 1 skips

    Args:
        state: TopLevelState
        override: Optional path override from run_config (Layer 2)
        prefer_stages: Optional list of stage names to check first, in order (Layer 3)

    Returns:
        (assembly_path, source)
        source: "run_config" | "<stage_name>" | "asset_registry" | "missing"

    Examples:
        # hic_scaffolding: prefer assembly_review (Layer 3 preference)
        assembly_ref, source = resolve_current_assembly(state, prefer_stages=["assembly_review"])

        # t2t: with run_config override (Layer 2) + Layer 3 preference
        assembly_ref, source = resolve_current_assembly(
            state,
            override=config.get("assembly_fasta_ref"),
            prefer_stages=["hic_scaffolding", "assembly_review"]
        )

        # When assembly/hic skipped via requested_stages (Layer 1), falls back to asset_registry
        assembly_ref, source = resolve_current_assembly(
            state,
            prefer_stages=["t2t", "hic_scaffolding", "assembly_review"]
        )
    """
    # Layer 2: Run config override (highest priority)
    if override:
        return str(override), "run_config"

    outputs = state.get("stage_outputs") or {}

    # Layer 3: Check preferred stages (in order)
    if prefer_stages:
        for stage in prefer_stages:
            stage_out = outputs.get(stage) or {}
            # Unified contract key (preferred)
            if stage_out.get("final_assembly_for_downstream"):
                return str(stage_out["final_assembly_for_downstream"]), stage
            # Legacy fallback (for backward compatibility)
            legacy_keys = _get_legacy_assembly_keys(stage)
            for key in legacy_keys:
                if stage_out.get(key):
                    return str(stage_out[key]), stage

    # Auto-fallback: check all stages in reverse STAGE_ORDER
    from app.stages import STAGE_ORDER

    for stage in reversed(STAGE_ORDER):
        if prefer_stages and stage in prefer_stages:
            continue  # Already checked above
        stage_out = outputs.get(stage) or {}
        if stage_out.get("final_assembly_for_downstream"):
            return str(stage_out["final_assembly_for_downstream"]), stage

    # Layer 1 support: Fallback to asset registry (user-provided assembly)
    # This handles scenarios where assembly stages were skipped via requested_stages
    assembly_ref = _load_assembly_from_registry(state)
    if assembly_ref:
        return assembly_ref, "asset_registry"

    return None, "missing"


def _get_legacy_assembly_keys(stage: str) -> list[str]:
    """Return legacy output keys for a stage, in priority order."""
    legacy_map = {
        "assembly_review": ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"],
        "hic_scaffolding": ["final_assembly_for_annotation", "chromosome_scaffolded_assembly_candidate_ref"],
        "t2t": ["final_assembly_for_annotation"],
    }
    return legacy_map.get(stage, [])


def _load_assembly_from_registry(state: TopLevelState) -> str | None:
    """Load assembly_fasta_registered from asset registry.

    This supports Layer 1 (TUI/Plan) control: when assembly stages are skipped
    via requested_stages, downstream stages can still find user-provided assemblies.
    """
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return None

    registry_path = Path(str(registry_ref))
    if not registry_path.exists():
        return None

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for asset in registry.get("assets", []):
            if asset.get("asset_type") == "assembly_fasta_registered" and asset.get("path"):
                return str(asset["path"])
    except (OSError, json.JSONDecodeError):
        pass

    return None
