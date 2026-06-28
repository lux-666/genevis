"""Preflight validation API for TUI stage planning.

This module provides read-only validation functions to check if stages can run
given the current state. Used by TUI to validate execution plans before starting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.state import TopLevelState
from app.stages.base import ResourceSpec, resolve_stage_resource


@dataclass
class ResourceCheck:
    """Result of checking one resource requirement."""

    resource_name: str
    """Human-readable resource name (e.g., 'assembly', 'clean reads')."""

    spec: ResourceSpec
    """The resource specification that was checked."""

    found: bool
    """Whether the resource was found."""

    source: str | None
    """Where it was found ('run_config', stage name, 'asset_registry', or None)."""

    message: str
    """Human-readable explanation of the result."""


@dataclass
class StageCheck:
    """Result of checking one stage's prerequisites."""

    stage: str
    """Stage name."""

    can_run: bool
    """Whether this stage can run with current inputs."""

    missing: list[ResourceCheck]
    """Resources that are missing (if any)."""

    warnings: list[str] = field(default_factory=list)
    """Non-blocking warnings."""

    suggestions: list[str] = field(default_factory=list)
    """Suggestions for resolving issues."""


@dataclass
class PlanValidation:
    """Result of validating an execution plan."""

    valid: bool
    """Whether all requested stages can run."""

    checks: dict[str, StageCheck]
    """Per-stage check results: {stage_name: StageCheck}."""

    first_blocker: str | None
    """First stage that blocks execution (if any)."""

    summary: str
    """Human-readable summary."""

    suggestions: list[str] = field(default_factory=list)
    """Overall suggestions for fixing the plan."""


def check_stage_prerequisites(
    state: TopLevelState,
    stage: str,
) -> StageCheck:
    """
    Check if a stage can run given current state.

    This is a READ-ONLY check for TUI planning.
    Does not modify state.

    Args:
        state: Current state (with asset_registry_ref, stage_outputs)
        stage: Stage name to check

    Returns:
        StageCheck with can_run, missing resources, suggestions
    """
    requirements = _get_stage_requirements(stage)
    if not requirements:
        # Stage has no declared requirements, assume it can run
        return StageCheck(
            stage=stage,
            can_run=True,
            missing=[],
            warnings=[f"No requirements defined for stage '{stage}'"],
        )

    missing_resources: list[ResourceCheck] = []
    warnings: list[str] = []

    for req in requirements:
        spec = req["spec"]
        override = req.get("override")

        resolution = resolve_stage_resource(state, resource_spec=spec, override=override)

        if not resolution.found:
            if spec.required:
                missing_resources.append(ResourceCheck(
                    resource_name=req["name"],
                    spec=spec,
                    found=False,
                    source=None,
                    message=f"Required {req['name']} not found. Searched: {', '.join(spec.prefer_stages or ['asset_registry'])}.",
                ))
            else:
                warnings.append(f"Optional {req['name']} not found (will use fallback).")

    can_run = len(missing_resources) == 0
    suggestions = []

    if not can_run:
        suggestions = _generate_suggestions(stage, missing_resources)

    return StageCheck(
        stage=stage,
        can_run=can_run,
        missing=missing_resources,
        warnings=warnings,
        suggestions=suggestions,
    )


def validate_execution_plan(
    state: TopLevelState,
    requested_stages: list[str],
) -> PlanValidation:
    """
    Validate if requested stages can run.

    Checks stage prerequisites in dependency order (STAGE_ORDER).
    Stops at first blocking issue.

    Args:
        state: Initial state (inventory completed)
        requested_stages: Stages user wants to run

    Returns:
        PlanValidation with overall status, per-stage checks, suggestions
    """
    from app.stages import STAGE_ORDER

    # Check stages in dependency order
    checks: dict[str, StageCheck] = {}
    first_blocker: str | None = None

    for stage in STAGE_ORDER:
        if stage not in requested_stages:
            continue

        check = check_stage_prerequisites(state, stage)
        checks[stage] = check

        if not check.can_run and first_blocker is None:
            first_blocker = stage

    valid = first_blocker is None
    summary = _build_summary(valid, first_blocker, checks)
    suggestions = _build_overall_suggestions(checks, first_blocker)

    return PlanValidation(
        valid=valid,
        checks=checks,
        first_blocker=first_blocker,
        summary=summary,
        suggestions=suggestions,
    )


def _get_stage_requirements(stage: str) -> list[dict[str, Any]]:
    """
    Get resource requirements for a stage.

    TODO: Future enhancement - generate via LLM + knowledge base.
    Current: Hardcoded basic requirements.

    Returns:
        List of requirement dicts with 'name', 'spec', optional 'override'
    """
    # Import here to avoid circular dependency
    from app.stages.resource_specs import STAGE_REQUIREMENTS

    return STAGE_REQUIREMENTS.get(stage, [])


def _generate_suggestions(stage: str, missing: list[ResourceCheck]) -> list[str]:
    """Generate suggestions for resolving missing resources."""
    suggestions = []

    for res in missing:
        if res.resource_name == "assembly":
            suggestions.append(
                "Provide 'assembly_fasta_registered' asset in manifest, "
                "or add upstream stages: assembly → assembly_review"
            )
        elif res.resource_name == "reads" or res.resource_name == "raw reads":
            suggestions.append(
                "Provide 'raw_long_reads' asset in manifest"
            )
        elif res.resource_name == "clean reads":
            suggestions.append(
                "Provide 'clean_long_reads' asset in manifest, "
                "or add 'read_qc' stage to generate clean reads"
            )

    return suggestions


def _build_summary(valid: bool, first_blocker: str | None, checks: dict[str, StageCheck]) -> str:
    """Build human-readable summary."""
    if valid:
        return f"✓ All {len(checks)} requested stages can run."

    blocker_check = checks.get(first_blocker) if first_blocker else None
    if blocker_check:
        missing_names = [r.resource_name for r in blocker_check.missing]
        return f"✗ Cannot run '{first_blocker}': missing {', '.join(missing_names)}."

    return "✗ Execution plan validation failed."


def _build_overall_suggestions(checks: dict[str, StageCheck], first_blocker: str | None) -> list[str]:
    """Build overall suggestions for the plan."""
    if not first_blocker:
        return []

    blocker_check = checks.get(first_blocker)
    if blocker_check and blocker_check.suggestions:
        return blocker_check.suggestions

    return ["Review your manifest and ensure all required assets are provided."]
