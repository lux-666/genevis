"""Shared parameter source resolution with fixed L2 > L1 > L0 precedence.

Accepts a MethodParameterCatalog, typed L0 values, a
RecommendationInputSnapshotV3, and the approved sparse L2 mapping.  Returns
a provenance-separated ResolvedParameterSources.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from app.domains.parameter_recommendation.facts import (
    apply_fact_transform,
    resolve_canonical_fact,
)
from app.schemas.parameter_catalog import (
    ChoiceOptionApplication,
    CliOptionApplication,
    CliSwitchApplication,
    ConfigKeyApplication,
    FactSource,
    FixedExecutorSource,
    MaterializedDefaultExecutorSource,
    MethodParameterCatalog,
    ParameterSpec,
    PositionalApplication,
    RuntimeExecutorSource,
    SelectorApplication,
    StageValueApplication,
)
from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    CanonicalRecommendationFacts,
    RecommendationInputSnapshotV3,
    ResolvedParameterSources,
)

# ---------------------------------------------------------------------------
# Executor binding registry
# ---------------------------------------------------------------------------

_ExecutorValues = dict[str, Any]


def resolve_parameters(
    catalog: MethodParameterCatalog,
    *,
    l0_executor_values: _ExecutorValues,
    snapshot: RecommendationInputSnapshotV3,
    approved_l2: dict[str, Any],
) -> ResolvedParameterSources:
    """Resolve all catalog parameters into provenance-separated output.

    Precedence (highest to lowest):
        approved L2 value
          > resolved L1 fact
          > declared/materialized L0 value
          > implicit tool default (parameter remains absent)

    Raises ValueError when a required parameter has no effective value, or
    when a supplied L0 binding or approved L2 key is not declared by the
    catalog.
    """

    declared_l0_bindings = {
        source.binding
        for spec in catalog.parameters.values()
        if isinstance((source := spec.layers.l0), RuntimeExecutorSource)
    }
    undeclared_l0_bindings = sorted(
        set(l0_executor_values).difference(declared_l0_bindings)
    )
    if undeclared_l0_bindings:
        raise ValueError(
            f"supplied L0 executor binding(s) {undeclared_l0_bindings!r} are not "
            f"declared by catalog {catalog.catalog_id!r}"
        )

    # Validate approved L2 keys are all cataloged as L2.
    for key in approved_l2:
        spec = catalog.parameters.get(key)
        if spec is None or spec.layers.l2 is None:
            raise ValueError(
                f"approved L2 key {key!r} is not cataloged as L2-adjustable "
                f"in catalog {catalog.catalog_id!r}"
            )

    facts = snapshot.facts
    stage_ctx = snapshot.stage_context

    l0_out: dict[str, Any] = {}
    l1_out: dict[str, Any] = {}
    l2_out: dict[str, Any] = {}
    effective: dict[str, Any] = {}
    source_by: dict[str, Literal["l0", "l1", "l2"]] = {}
    applications: dict[str, Any] = {}

    for name, spec in catalog.parameters.items():
        layers = spec.layers
        app = spec.application
        applications[name] = app.model_dump()

        # --- Candidate values ---
        l2_candidate: tuple[Any, bool] = (None, False)  # (value, present)
        l1_candidate: tuple[Any, bool] = (None, False)
        l0_candidate: tuple[Any, bool] = (None, False)

        # L2: approved sparse mapping
        if layers.l2 is not None and name in approved_l2:
            l2_candidate = (approved_l2[name], True)
            l2_out[name] = approved_l2[name]

        # L1: canonical fact lookup
        if layers.l1 is not None:
            raw = _resolve_l1_fact(layers.l1, facts, stage_ctx)
            if raw is not None:
                l1_candidate = (raw, True)
                l1_out[name] = raw

        # L0: executor binding or materialized default
        if layers.l0 is not None:
            raw_l0 = _resolve_l0_source(layers.l0, l0_executor_values, name)
            if raw_l0 is not None:
                l0_candidate = (raw_l0, True)
                l0_out[name] = raw_l0

        # --- Choose effective value by precedence ---
        if l2_candidate[1]:
            _validate_resolved_catalog_value(
                name,
                spec,
                l2_candidate[0],
                source="l2",
            )
            effective[name] = l2_candidate[0]
            source_by[name] = "l2"
        elif l1_candidate[1]:
            _validate_resolved_catalog_value(
                name,
                spec,
                l1_candidate[0],
                source="l1",
            )
            effective[name] = l1_candidate[0]
            source_by[name] = "l1"
        elif l0_candidate[1]:
            _validate_resolved_catalog_value(
                name,
                spec,
                l0_candidate[0],
                source="l0",
            )
            effective[name] = l0_candidate[0]
            source_by[name] = "l0"

        # --- required_for_execution check ---
        if spec.required_for_execution and name not in effective:
            raise ValueError(
                f"required_for_execution parameter {name!r} has no effective value "
                f"in catalog {catalog.catalog_id!r}"
            )

    return ResolvedParameterSources(
        l0_executor=l0_out,
        l1_facts=l1_out,
        l2_approved=l2_out,
        effective_parameters=effective,
        effective_source_by_parameter=source_by,
        applications=applications,
    )


# ---------------------------------------------------------------------------
# Catalog value validation
# ---------------------------------------------------------------------------


def _validate_resolved_catalog_value(
    name: str,
    spec: ParameterSpec,
    value: Any,
    *,
    source: Literal["l0", "l1", "l2"],
) -> None:
    """Validate one selected value without coercion or fallback repair."""

    context = f"parameter {name!r} resolved from {source.upper()}"
    value_type = spec.value_type
    render_error = (
        f"{name} cannot be rendered as {value_type}: {value!r} "
        f"(resolved from {source.upper()})"
    )

    if value_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(render_error)
    elif value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(render_error)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{context} must be a finite float: {value!r}")
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(render_error)
    elif value_type == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(render_error)
    elif value_type == "path":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(render_error)
    elif value_type == "path_list":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise ValueError(render_error)

    if spec.min is not None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"{context} cannot be checked against minimum {spec.min!r}"
            )
        if value < spec.min:
            raise ValueError(f"{context} value {value!r} is below minimum {spec.min!r}")

    if spec.max is not None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"{context} cannot be checked against maximum {spec.max!r}"
            )
        if value > spec.max:
            raise ValueError(f"{context} value {value!r} is above maximum {spec.max!r}")

    if spec.allowed_values is not None and value not in spec.allowed_values:
        raise ValueError(
            f"{context} value {value!r} is not in allowed_values "
            f"{spec.allowed_values!r}"
        )


# ---------------------------------------------------------------------------
# L0 resolver
# ---------------------------------------------------------------------------


def _resolve_l0_source(
    source: Any,
    executor_values: _ExecutorValues,
    param_name: str,
) -> Any:
    if isinstance(source, FixedExecutorSource):
        return source.value
    if isinstance(source, MaterializedDefaultExecutorSource):
        return source.value
    if isinstance(source, RuntimeExecutorSource):
        binding = source.binding
        if binding in executor_values:
            return executor_values[binding]
        if source.materialized_default is not None:
            return source.materialized_default
        if source.required:
            raise ValueError(
                f"required executor binding {binding!r} is absent for parameter "
                f"{param_name!r}"
            )
        return None
    return None


# ---------------------------------------------------------------------------
# L1 resolver
# ---------------------------------------------------------------------------


def _resolve_l1_fact(
    fact_source: FactSource,
    facts: CanonicalRecommendationFacts,
    stage_ctx: Any,
) -> Any:
    key = fact_source.fact
    transform = fact_source.transform

    # Stage-specific fact: hic_provided_restriction_enzyme from AutoHiC context
    if key == "hic_provided_restriction_enzyme":
        if isinstance(stage_ctx, AutoHiCRecommendationContext):
            raw = stage_ctx.provided_restriction_enzyme
        else:
            raw = None
    else:
        raw = resolve_canonical_fact(key, facts)

    if raw is None:
        return None
    return apply_fact_transform(transform, raw)


# ---------------------------------------------------------------------------
# CLI/config rendering helpers
# ---------------------------------------------------------------------------


def render_cli_argv(
    resolved: ResolvedParameterSources,
    catalog: MethodParameterCatalog,
) -> list[str]:
    """Render CLI arguments from resolved parameters in declared order.

    Visits every parameter exactly once.  Does not concatenate source groups.
    """

    # Collect CLI-emitting specs in render order
    ordered: list[tuple[int, str, ParameterSpec]] = []
    selector_params: dict[str, str] = {}  # param_name -> current value

    for name, spec in catalog.parameters.items():
        app = spec.application
        if isinstance(app, SelectorApplication):
            # Capture selector values to resolve choice_options
            if name in resolved.effective_parameters:
                selector_params[name] = str(resolved.effective_parameters[name])
        elif isinstance(
            app,
            (CliOptionApplication, CliSwitchApplication, ChoiceOptionApplication),
        ):
            ordered.append((app.order, name, spec))
        elif isinstance(app, PositionalApplication):
            ordered.append((10000 + app.position, name, spec))

    ordered.sort(key=lambda t: t[0])

    argv: list[str] = []
    for _, name, spec in ordered:
        if name not in resolved.effective_parameters:
            continue
        value = resolved.effective_parameters[name]
        app = spec.application

        if isinstance(app, CliOptionApplication):
            argv.extend(_render_cli_option(app.flag, value, app.join))
        elif isinstance(app, CliSwitchApplication):
            if value is True:
                argv.append(app.flag)
        elif isinstance(app, ChoiceOptionApplication):
            selector_val = selector_params.get(app.selector)
            if selector_val is None:
                continue
            flag = app.flags.get(selector_val)
            if flag is None:
                continue
            argv.append(flag)
            argv.extend(_render_values(value, app.join))
        elif isinstance(app, PositionalApplication):
            argv.extend(_render_values(value, "expand"))

    return argv


def _render_cli_option(flag: str, value: Any, join: str) -> list[str]:
    parts = [flag]
    parts.extend(_render_values(value, join))
    return parts


def _render_values(value: Any, join: str) -> list[str]:
    if isinstance(value, list):
        if join == "expand":
            return [str(v) for v in value]
        if join == "comma":
            return [",".join(str(v) for v in value)]
        return [str(value[0])] if value else []
    return [str(value)]


def render_config_keys(
    resolved: ResolvedParameterSources,
    catalog: MethodParameterCatalog,
) -> dict[str, Any]:
    """Return a dict of config_key -> value for stage-owned config writers."""

    result: dict[str, Any] = {}
    for name, spec in catalog.parameters.items():
        if not isinstance(spec.application, ConfigKeyApplication):
            continue
        if name in resolved.effective_parameters:
            result[spec.application.key] = resolved.effective_parameters[name]
    return result


def render_stage_values(
    resolved: ResolvedParameterSources,
    catalog: MethodParameterCatalog,
) -> dict[str, Any]:
    """Return a dict of stage_value key -> value for T2T-style execution."""

    result: dict[str, Any] = {}
    for name, spec in catalog.parameters.items():
        if not isinstance(spec.application, StageValueApplication):
            continue
        if name in resolved.effective_parameters:
            result[spec.application.key] = resolved.effective_parameters[name]
    return result
