"""Build deterministic assembly commands from L0/L1/L2 parameter sources."""

from __future__ import annotations

from typing import Any

from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
from app.schemas.state import TopLevelState

SOURCE_GROUP_LEVELS = {
    "l0_fixed": 0,
    "l1_readonly": 1,
    "l2_recommended": 2,
}


class ParameterCommandError(ValueError):
    """Raised when parameter sources cannot be converted to a safe argv."""


def build_command(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    parameter_sources: dict[str, dict[str, Any]],
) -> list[str]:
    """Merge L0/L1/L2 source groups into a deterministic argv."""

    _validate_groups(catalog, parameter_sources)
    argv = [catalog.tool]
    positional: list[str] = []

    for group_name in ("l0_fixed", "l1_readonly", "l2_recommended"):
        group = parameter_sources.get(group_name) or {}
        keys = _ordered_keys(catalog, group, fixed_booleans_first=group_name == "l0_fixed")
        for key in keys:
            value = group[key]
            spec = catalog.parameters[key]
            emitted, positional_values = _emit_parameter(spec, value)
            argv.extend(emitted)
            positional.extend(positional_values)

    argv.extend(positional)
    return argv


def build_parameter_sources(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    state: TopLevelState,
    assembly_out: dict[str, Any],
    attempt_id: int,
    recommended_params: dict[str, Any],
    execution_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build replayable parameter source groups for hifiasm methods."""

    # TODO: Architecture improvement needed - execution_context keys are hardcoded
    # (output_prefix, threads, input_reads). These should be driven by the catalog's
    # source field declarations rather than implicitly known by callers.
    # See phase10 architecture review for details.

    execution_context = execution_context or {}
    l0_fixed: dict[str, Any] = {}
    l1_readonly: dict[str, Any] = {}

    for key, spec in catalog.parameters.items():
        if spec.level == 0 and spec.fixed_value is not None:
            l0_fixed[key] = spec.fixed_value
        elif spec.level == 0:
            value = _first_available_value(spec.source_priority or [], state, assembly_out, execution_context)
            if value is not None:
                l0_fixed[key] = value
        elif spec.level == 1:
            value = _first_available_value(spec.source_priority or [], state, assembly_out, execution_context)
            if value is not None:
                l1_readonly[key] = value

    return {
        "l0_fixed": l0_fixed,
        "l1_readonly": l1_readonly,
        "l2_recommended": dict(recommended_params),
    }


def _validate_groups(catalog: MethodParameterCatalog, parameter_sources: dict[str, dict[str, Any]]) -> None:
    unknown_groups = sorted(set(parameter_sources) - set(SOURCE_GROUP_LEVELS))
    if unknown_groups:
        raise ParameterCommandError(f"Unknown parameter source group(s): {unknown_groups}")
    for group_name, expected_level in SOURCE_GROUP_LEVELS.items():
        group = parameter_sources.get(group_name) or {}
        if not isinstance(group, dict):
            raise ParameterCommandError(f"{group_name} must be a mapping")
        for key in group:
            spec = catalog.parameters.get(key)
            if spec is None:
                raise ParameterCommandError(f"Unknown parameter for {group_name}: {key}")
            if spec.level != expected_level:
                raise ParameterCommandError(f"{key} belongs to L{spec.level}, not {group_name}")


def _ordered_keys(
    catalog: MethodParameterCatalog,
    group: dict[str, Any],
    *,
    fixed_booleans_first: bool,
) -> list[str]:
    catalog_keys = [key for key in catalog.parameters if key in group]
    if not fixed_booleans_first:
        return catalog_keys
    return sorted(
        catalog_keys,
        key=lambda key: (
            not _is_fixed_truthy_boolean(catalog.parameters[key], group[key]),
            list(catalog.parameters).index(key),
        ),
    )


def _is_fixed_truthy_boolean(spec: ParameterSpec, value: Any) -> bool:
    return spec.value_type == "boolean" and bool(value) and spec.fixed_value is not None


def _emit_parameter(spec: ParameterSpec, value: Any) -> tuple[list[str], list[str]]:
    if value is None or value == "" or value == "default":
        return [], []
    if spec.value_type == "boolean":
        if not value:
            return [], []
        return ([spec.cli] if spec.cli else []), []
    if spec.cli is None:
        if isinstance(value, list):
            return [], [str(item) for item in value]
        return [], [str(value)]
    if spec.value_type == "path_list" and isinstance(value, list):
        return [spec.cli, ",".join(str(item) for item in value)], []
    return [spec.cli, str(value)], []


def _first_available_value(
    paths: list[str],
    state: TopLevelState,
    assembly_out: dict[str, Any],
    execution_context: dict[str, Any],
) -> Any:
    roots: dict[str, Any] = {
        "state": state,
        "assembly": assembly_out,
        "execution": execution_context,
        "shared_parameters": state.get("shared_parameters") or {},
    }
    stage_outputs = state.get("stage_outputs") or {}
    roots.update(stage_outputs)
    for path in paths:
        value = _lookup_path(path, roots)
        if value is not None:
            return value
    return None


def _lookup_path(path: str, roots: dict[str, Any]) -> Any:
    parts = path.split(".")
    if not parts:
        return None
    current: Any = roots.get(parts[0])
    for part in parts[1:]:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current
