"""Assembly adapters for shared parameter resolution and rendering."""

from __future__ import annotations

from typing import Any

from app.domains.parameter_recommendation.resolution import (
    render_cli_argv,
    render_config_keys,
    resolve_parameters,
)
from app.schemas.parameter_catalog import (
    ChoiceOptionApplication,
    MethodParameterCatalog,
    SelectorApplication,
)
from app.schemas.parameter_recommendation import (
    RecommendationInputSnapshotV3,
    ResolvedParameterSources,
)


class ParameterCommandError(ValueError):
    """Raised when approved parameter sources cannot be rendered safely."""


def resolve_assembly_parameters(
    *,
    catalog: MethodParameterCatalog,
    l0_executor_values: dict[str, Any],
    snapshot: RecommendationInputSnapshotV3,
    approved_l2: dict[str, Any],
) -> ResolvedParameterSources:
    """Resolve one approved Assembly attempt from its persisted v3 snapshot."""

    if snapshot.method_id != catalog.method_id:
        raise ParameterCommandError(
            f"parameter snapshot method {snapshot.method_id!r} does not match "
            f"catalog {catalog.method_id!r}"
        )
    try:
        resolved = resolve_parameters(
            catalog,
            l0_executor_values=l0_executor_values,
            snapshot=snapshot,
            approved_l2=approved_l2,
        )
    except ValueError as exc:
        raise ParameterCommandError(str(exc)) from exc
    validate_renderable_parameters(catalog, resolved)
    return resolved


def render_assembly_cli(
    catalog: MethodParameterCatalog,
    resolved: ResolvedParameterSources,
) -> list[str]:
    """Render a complete Assembly CLI command from resolved parameters."""

    validate_renderable_parameters(catalog, resolved)
    return [catalog.tool, *render_cli_argv(resolved, catalog)]


def render_assembly_config(
    catalog: MethodParameterCatalog,
    resolved: ResolvedParameterSources,
) -> dict[str, Any]:
    """Render the typed values consumed by an Assembly config writer."""

    validate_renderable_parameters(catalog, resolved)
    return render_config_keys(resolved, catalog)


def parameter_sources_payload(resolved: ResolvedParameterSources) -> dict[str, Any]:
    """Return all six canonical source views for frozen evidence."""

    return resolved.model_dump(mode="json")


def validate_renderable_parameters(
    catalog: MethodParameterCatalog,
    resolved: ResolvedParameterSources,
) -> None:
    """Reject values that cannot be represented by their declared application."""

    for name, value in resolved.effective_parameters.items():
        spec = catalog.parameters.get(name)
        if spec is None:
            raise ParameterCommandError(f"Unknown resolved parameter: {name}")
        if not _is_renderable_value(spec.value_type, value):
            raise ParameterCommandError(
                f"{name} cannot be rendered as {spec.value_type}: {value!r}"
            )

    for name, spec in catalog.parameters.items():
        if not isinstance(spec.application, SelectorApplication):
            continue
        if name not in resolved.effective_parameters:
            continue
        selector_value = str(resolved.effective_parameters[name])
        choice = next(
            (
                candidate.application
                for candidate in catalog.parameters.values()
                if isinstance(candidate.application, ChoiceOptionApplication)
                and candidate.application.selector == name
            ),
            None,
        )
        if choice is None or selector_value not in choice.flags:
            raise ParameterCommandError(
                f"{name} selector value has no declared CLI choice: {selector_value!r}"
            )


def _is_renderable_value(value_type: str, value: Any) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type in {"string", "path"}:
        return isinstance(value, str) and bool(value)
    if value_type == "path_list":
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and bool(item) for item in value)
        )
    return False
