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
    planned_resources: dict[str, list[str]] | None = None,
    stages_processed: set[str] | None = None,
) -> StageCheck:
    """
    Check if a stage can run given current state.

    This is a READ-ONLY check for TUI planning.
    Does not modify state.

    Args:
        state: Current state (with asset_registry_ref, stage_outputs)
        stage: Stage name to check
        planned_resources: Resources that will be produced by upstream stages
                          in this plan: {stage_name: [output_key, ...]}
        stages_processed: Set of stages already processed (to avoid circular deps)

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
    planned_resources = planned_resources or {}
    stages_processed = stages_processed or set()

    for req in requirements:
        spec = req["spec"]
        override = req.get("override")

        # First check if any upstream stage in this plan will produce this resource
        found_in_plan = _check_planned_resource(spec, planned_resources, stages_processed, stage)

        if found_in_plan:
            # This resource will be produced by an upstream stage in the plan
            continue

        # Otherwise check current state
        resolution = resolve_stage_resource(state, resource_spec=spec, override=override)

        if not resolution.found:
            if spec.required or _is_contextually_required(state, stage, spec):
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
    Simulates resource production from upstream stages in the plan.

    Args:
        state: Initial state (inventory completed)
        requested_stages: Stages user wants to run

    Returns:
        PlanValidation with overall status, per-stage checks, suggestions
    """
    from app.stages import STAGE_ORDER
    import copy

    # Build a simulated state that accumulates resources from planned stages
    simulated_state = copy.deepcopy(state)
    produced_resources = _collect_planned_resources(requested_stages)
    stages_processed: set[str] = set()

    # Check stages in dependency order
    checks: dict[str, StageCheck] = {}
    first_blocker: str | None = None

    for stage in STAGE_ORDER:
        if stage not in requested_stages:
            continue

        check = check_stage_prerequisites(simulated_state, stage, produced_resources, stages_processed)
        checks[stage] = check

        if not check.can_run and first_blocker is None:
            first_blocker = stage

        # Simulate this stage's outputs being added to state
        if check.can_run:
            _simulate_stage_outputs(simulated_state, stage)
            stages_processed.add(stage)

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
        elif res.resource_name == "masked assembly":
            suggestions.append(
                "Add upstream stage 'repeat_annotation' to produce a softmasked genome, "
                "or provide an explicit repeat_annotation skip/fallback reason."
            )
        elif res.resource_name == "predicted proteins":
            suggestions.append(
                "Add upstream stage 'gene_annotation' to produce predicted proteins before function_annotation."
            )

    return suggestions


def _is_contextually_required(state: TopLevelState, stage: str, spec: ResourceSpec) -> bool:
    """Return whether an optional resource is required by this stage context."""
    if stage == "gene_annotation" and spec.resource_type == "masked_assembly":
        return not _has_unmasked_fallback_reason(state)
    return False


def _has_unmasked_fallback_reason(state: TopLevelState) -> bool:
    """Mirror gene_annotation runtime's explicit unmasked fallback gate."""
    outputs = state.get("stage_outputs") or {}
    gene_out = outputs.get("gene_annotation") or {}
    if gene_out.get("unmasked_fallback_reason"):
        return True
    repeat_out = outputs.get("repeat_annotation") or {}
    if repeat_out.get("unmasked_fallback_reason"):
        return True
    return bool((state.get("skip_reasons") or {}).get("repeat_annotation"))


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


def _collect_planned_resources(requested_stages: list[str]) -> dict[str, list[str]]:
    """
    Collect what resources each stage in the plan will produce.

    Returns:
        {stage_name: [output_key, ...]}
    """
    from app.stages.resource_specs import STAGE_OUTPUTS

    return {
        stage: outputs
        for stage in requested_stages
        if (outputs := STAGE_OUTPUTS.get(stage))
    }


def _check_planned_resource(
    spec: ResourceSpec,
    planned_resources: dict[str, list[str]],
    stages_processed: set[str],
    current_stage: str,
) -> bool:
    """
    Check if a ResourceSpec will be satisfied by upstream stages in the plan.

    Args:
        spec: The resource specification to check
        planned_resources: {stage_name: [output_key, ...]} from _collect_planned_resources
        stages_processed: Set of stages already checked/processed (upstream of current)
        current_stage: The stage we're currently checking (to avoid circular deps)

    Returns:
        True if an upstream stage in the plan will produce this resource
    """
    # Check if any preferred stage in the spec will run and produce a matching output key
    # BUT only consider stages that have already been processed (are upstream)
    for stage in spec.prefer_stages:
        if stage == current_stage:
            # A stage can't satisfy its own requirements
            continue
        if stage in planned_resources and stage in stages_processed:
            planned_outputs = planned_resources[stage]
            expected_outputs = spec.output_keys.get(stage, [])

            # If this stage will produce any of the expected output keys, resource will be available
            if any(out in planned_outputs for out in expected_outputs):
                return True

    return False


def _simulate_stage_outputs(state: TopLevelState, stage: str) -> None:
    """
    Simulate a stage having run by adding placeholder outputs to state.

    This mutates the state to help downstream stages in the same plan
    see that their inputs will be available.

    Args:
        state: State to mutate
        stage: Stage that just "ran"
    """
    # Add placeholder stage outputs
    if "stage_outputs" not in state:
        state["stage_outputs"] = {}

    from app.stages.resource_specs import STAGE_OUTPUTS

    outputs = {key: "placeholder" for key in STAGE_OUTPUTS.get(stage, [])}
    if outputs:
        state["stage_outputs"][stage] = outputs
