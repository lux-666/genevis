"""Build deterministic assembly commands from L0/L1/L2 parameter sources."""

from __future__ import annotations

import re
from typing import Any

from app.schemas.parameter_catalog import (
    CliOptionApplication,
    CliSwitchApplication,
    ChoiceOptionApplication,
    ConfigKeyApplication,
    FixedExecutorSource,
    MethodParameterCatalog,
    ParameterSpec,
    PositionalApplication,
    SelectorApplication,
    StageValueApplication,
)
from app.schemas.parameter_recommendation import RecommendationInputSnapshotV3

SOURCE_GROUP_LEVELS = {
    "l0_fixed": 0,
    "l1_readonly": 1,
    "l2_recommended": 2,
}

# Map from group name to the layer attribute to check on spec.layers.
_GROUP_LAYER_ATTR = {
    "l0_fixed": "l0",
    "l1_readonly": "l1",
    "l2_recommended": "l2",
}

# hifiasm --hg-size accepts INT with a required k/m/g unit suffix.
_HG_SIZE_WITH_UNIT_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>[kmgKMG])$")
_HG_SIZE_BARE_INT_RE = re.compile(r"^\d+$")
_HG_SIZE_BARE_FLOAT_RE = re.compile(r"^\d+\.\d+$")
_HG_SIZE_UNIT_MULTIPLIER = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


class ParameterCommandError(ValueError):
    """Raised when parameter sources cannot be converted to a safe argv."""


def format_hifiasm_hg_size(value: Any) -> str:
    """Normalize genome size for hifiasm ``--hg-size``.

    Accepts integer/float base-pair counts or strings such as ``"500m"`` / ``"1g"``
    / bare bp integers. Always returns a hifiasm-legal ``INT(k|m|g)`` string.
    """

    if value is None or value == "" or value == "default":
        raise ParameterCommandError(f"hg_size value is empty: {value!r}")

    if isinstance(value, bool):
        raise ParameterCommandError(f"hg_size must be a genome size, not boolean: {value!r}")

    if isinstance(value, (int, float)):
        if value <= 0:
            raise ParameterCommandError(f"hg_size must be positive base pairs: {value!r}")
        bp = int(value) if float(value).is_integer() else int(round(float(value)))
        if bp <= 0:
            raise ParameterCommandError(f"hg_size must be positive base pairs: {value!r}")
        return _bp_to_hifiasm_hg_size(bp)

    if not isinstance(value, str):
        raise ParameterCommandError(f"hg_size must be int/float/str, got {type(value).__name__}: {value!r}")

    text = value.strip()
    if not text:
        raise ParameterCommandError(f"hg_size value is empty: {value!r}")

    match = _HG_SIZE_WITH_UNIT_RE.fullmatch(text)
    if match:
        number = float(match.group("number"))
        unit = match.group("unit").lower()
        if number <= 0:
            raise ParameterCommandError(f"hg_size must be positive: {value!r}")
        # Pass through already-legal values with normalized unit case.
        return f"{_format_hg_size_number(number)}{unit}"

    if _HG_SIZE_BARE_INT_RE.fullmatch(text):
        bp = int(text)
        if bp <= 0:
            raise ParameterCommandError(f"hg_size must be positive base pairs: {value!r}")
        return _bp_to_hifiasm_hg_size(bp)

    if _HG_SIZE_BARE_FLOAT_RE.fullmatch(text):
        bp = int(round(float(text)))
        if bp <= 0:
            raise ParameterCommandError(f"hg_size must be positive base pairs: {value!r}")
        return _bp_to_hifiasm_hg_size(bp)

    raise ParameterCommandError(
        f"hg_size must be base-pair count or hifiasm form INT(k|m|g), got {value!r}"
    )


def _bp_to_hifiasm_hg_size(bp: int) -> str:
    """Convert a positive base-pair count into hifiasm ``--hg-size`` form."""

    if bp <= 0:
        raise ParameterCommandError(f"hg_size must be positive base pairs: {bp!r}")

    # Prefer the largest unit that keeps a compact, exact-or-3dp representation.
    for unit, mult in (("g", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if bp < mult and unit != "k":
            continue
        if bp % mult == 0:
            return f"{bp // mult}{unit}"
        if bp >= mult:
            scaled = bp / mult
            rounded = round(scaled, 3)
            # Accept the unit when rounding to 3 decimals reconstructs bp closely.
            if abs(rounded * mult - bp) <= 0.5:
                return f"{_format_hg_size_number(rounded)}{unit}"

    # Sub-kilobase genomes: still require a unit suffix for hifiasm.
    return f"{_format_hg_size_number(bp / 1_000)}k"


def _format_hg_size_number(number: float) -> str:
    if float(number).is_integer():
        return str(int(number))
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def build_command(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    parameter_sources: dict[str, dict[str, Any]],
) -> list[str]:
    """Merge L0/L1/L2 source groups into a deterministic argv."""

    validate_parameter_sources(catalog, parameter_sources)
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


def validate_parameter_sources(
    catalog: MethodParameterCatalog,
    parameter_sources: dict[str, dict[str, Any]],
) -> None:
    """Validate that parameter source groups match catalog layers."""

    _validate_groups(catalog, parameter_sources)


def build_parameter_sources_v3(
    *,
    catalog: MethodParameterCatalog,
    l0_executor_values: dict[str, Any],
    snapshot: RecommendationInputSnapshotV3,
    approved_l2: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build legacy-shaped parameter_sources dict from ResolvedParameterSources."""
    from app.domains.parameter_recommendation.resolution import resolve_parameters
    resolved = resolve_parameters(
        catalog,
        l0_executor_values=l0_executor_values,
        snapshot=snapshot,
        approved_l2=approved_l2,
    )
    return {
        "l0_fixed": resolved.l0_executor,
        "l1_readonly": resolved.l1_facts,
        "l2_recommended": resolved.l2_approved,
    }


def _build_parameter_sources_legacy(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    state: Any,
    assembly_out: dict[str, Any],
    attempt_id: int,
    recommended_params: dict[str, Any],
    execution_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Deprecated: uses old spec.level / _first_available_value patterns.

    Callers should migrate to build_parameter_sources_v3.
    """
    # Kept for backward compatibility only — do not add new callers.
    execution_context = execution_context or {}
    l0_fixed: dict[str, Any] = {}
    l1_readonly: dict[str, Any] = {}

    for key, spec in catalog.parameters.items():
        if spec.layers.l0 is not None:
            # L0 layer: use executor values or materialized default
            from app.schemas.parameter_catalog import FixedExecutorSource, MaterializedDefaultExecutorSource, RuntimeExecutorSource
            if isinstance(spec.layers.l0, FixedExecutorSource):
                l0_fixed[key] = spec.layers.l0.value
            elif isinstance(spec.layers.l0, MaterializedDefaultExecutorSource):
                l0_fixed[key] = spec.layers.l0.value
        elif spec.layers.l1 is not None:
            # L1 layer: caller must supply values; skip here
            pass

    return {
        "l0_fixed": l0_fixed,
        "l1_readonly": l1_readonly,
        "l2_recommended": dict(recommended_params),
    }


# Keep old name as alias for callers not yet migrated.
build_parameter_sources = _build_parameter_sources_legacy


def _validate_groups(catalog: MethodParameterCatalog, parameter_sources: dict[str, dict[str, Any]]) -> None:
    unknown_groups = sorted(set(parameter_sources) - set(SOURCE_GROUP_LEVELS))
    if unknown_groups:
        raise ParameterCommandError(f"Unknown parameter source group(s): {unknown_groups}")
    for group_name in SOURCE_GROUP_LEVELS:
        group = parameter_sources.get(group_name) or {}
        if not isinstance(group, dict):
            raise ParameterCommandError(f"{group_name} must be a mapping")
        layer_attr = _GROUP_LAYER_ATTR[group_name]
        for key in group:
            spec = catalog.parameters.get(key)
            if spec is None:
                raise ParameterCommandError(f"Unknown parameter for {group_name}: {key}")
            if getattr(spec.layers, layer_attr) is None:
                raise ParameterCommandError(
                    f"{key} has no {layer_attr.upper()} layer, cannot appear in {group_name}"
                )
            if group_name == "l2_recommended":
                _validate_renderable_l2_value(key, spec, group[key])


def _validate_renderable_l2_value(
    key: str,
    spec: ParameterSpec,
    value: Any,
) -> None:
    """Reject only values that cannot be rendered as the declared CLI type.

    This runs after human approval.  It deliberately does not enforce catalog
    enums, ranges, or biological rules and never substitutes a default.
    """

    valid = True
    if spec.value_type == "boolean":
        valid = isinstance(value, bool)
    elif spec.value_type == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif spec.value_type == "float":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif spec.value_type in {"string", "path"}:
        valid = isinstance(value, str) and bool(value)
    elif spec.value_type == "path_list":
        valid = (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and bool(item) for item in value)
        )
    if not valid:
        raise ParameterCommandError(
            f"{key} cannot be rendered as {spec.value_type}: {value!r}"
        )


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
    return (
        spec.value_type == "boolean"
        and bool(value)
        and isinstance(spec.layers.l0, FixedExecutorSource)
    )


def _emit_parameter(spec: ParameterSpec, value: Any) -> tuple[list[str], list[str]]:
    if value is None or value == "":
        return [], []

    app = spec.application

    if isinstance(app, CliSwitchApplication):
        if not value:
            return [], []
        return [app.flag], []

    if isinstance(app, CliOptionApplication):
        # Detect whether the hifiasm_hg_size transform already formatted the value.
        already_formatted = (
            spec.layers.l1 is not None
            and spec.layers.l1.transform == "hifiasm_hg_size"
        )
        if app.flag == "--hg-size" and not already_formatted:
            value = format_hifiasm_hg_size(value)
        if app.join == "comma" and isinstance(value, list):
            return [app.flag, ",".join(str(item) for item in value)], []
        if app.join == "expand" and isinstance(value, list):
            result: list[str] = []
            for item in value:
                result.extend([app.flag, str(item)])
            return result, []
        return [app.flag, str(value)], []

    if isinstance(app, PositionalApplication):
        if isinstance(value, list):
            return [], [str(item) for item in value]
        return [], [str(value)]

    # SelectorApplication, ConfigKeyApplication, StageValueApplication,
    # ChoiceOptionApplication — not emitted in basic argv builder.
    return [], []


def _first_available_value(
    paths: list[str],
    state: Any,
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
