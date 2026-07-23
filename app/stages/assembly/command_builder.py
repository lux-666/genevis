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
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    RecommendationInputSnapshotV3,
)


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
    """Build parameter_sources dict from ResolvedParameterSources.

    Each parameter appears in exactly one group — its effective source level.
    This prevents dual-layer params from being emitted twice by build_command.
    """
    from app.domains.parameter_recommendation.resolution import resolve_parameters
    resolved = resolve_parameters(
        catalog,
        l0_executor_values=l0_executor_values,
        snapshot=snapshot,
        approved_l2=approved_l2,
    )
    src = resolved.effective_source_by_parameter
    return {
        "l0_fixed": {k: v for k, v in resolved.l0_executor.items() if src.get(k) == "l0"},
        "l1_readonly": {k: v for k, v in resolved.l1_facts.items() if src.get(k) == "l1"},
        "l2_recommended": {k: v for k, v in resolved.l2_approved.items() if src.get(k) == "l2"},
    }


def build_parameter_sources_from_state(
    *,
    method_id: str,
    catalog: "MethodParameterCatalog",
    state: "TopLevelState",
    assembly_out: dict,
    attempt_id: int,
    recommended_params: dict,
    execution_context: dict | None = None,
) -> "dict[str, dict]":
    """Build parameter_sources for method_application callers using the v3 resolver.

    Maps execution_context keys to ExecutorBindingKey values, builds a minimal
    RecommendationInputSnapshotV3 from state, and delegates to resolve_parameters.
    """
    from app.domains.parameter_recommendation.facts import build_canonical_facts
    from app.domains.parameter_recommendation.resolution import resolve_parameters
    from app.schemas.parameter_recommendation import (
        AssemblyRecommendationContext,
        RecommendationDependencyRefs,
        RecommendationInputSnapshotV3,
    )
    from app.stages.assembly.parameters.context import build_assembly_recommendation_context
    from app.schemas.artifacts import ParameterAttemptTask

    ctx = execution_context or {}
    # Map execution_context keys to ExecutorBindingKey values
    l0_executor_values: dict = {}
    if "output_prefix" in ctx:
        l0_executor_values["assembly_output_prefix"] = ctx["output_prefix"]
    if "output_dir" in ctx:
        l0_executor_values["assembly_output_dir"] = ctx["output_dir"]
    if "input_reads" in ctx:
        l0_executor_values["assembly_input_reads"] = ctx["input_reads"]
    elif "input_fofn" in ctx:
        l0_executor_values["assembly_input_reads"] = ctx["input_fofn"]
    if "read_type" in ctx:
        l0_executor_values["assembly_method_read_type"] = ctx["read_type"]
    elif "method_read_type" in ctx:
        l0_executor_values["assembly_method_read_type"] = ctx["method_read_type"]
    # Threads: from run_config or state shared_parameters
    shared = state.get("shared_parameters") or {}
    threads = (
        (shared.get("assembly") or {}).get("threads")
        or (state.get("run_config") or {}).get("threads")
        or 32
    )
    l0_executor_values["assembly_threads"] = int(threads)

    stage_ctx: AssemblyRecommendationContext = build_assembly_recommendation_context(
        state, assembly_out=assembly_out
    )
    facts = build_canonical_facts(
        shared_parameters=dict(shared),
        read_qc_output=dict((state.get("stage_outputs") or {}).get("read_qc") or {}),
        genome_survey_result=dict((state.get("stage_outputs") or {}).get("genome_survey") or {}),
        asset_flags={
            "has_hifi": assembly_out.get("input_profile", {}).get("has_hifi"),
            "has_ont": assembly_out.get("input_profile", {}).get("has_ont"),
            "has_hic": assembly_out.get("input_profile", {}).get("has_hic"),
        },
    )
    snapshot = RecommendationInputSnapshotV3(
        method_id=catalog.method_id,
        task="recommend_initial",
        facts=facts,
        stage_context=stage_ctx,
        dependency_refs=RecommendationDependencyRefs(),
    )
    resolved = resolve_parameters(
        catalog,
        l0_executor_values=l0_executor_values,
        snapshot=snapshot,
        approved_l2=dict(recommended_params),
    )
    src = resolved.effective_source_by_parameter
    return {
        "l0_fixed": {k: v for k, v in resolved.l0_executor.items() if src.get(k) == "l0"},
        "l1_readonly": {k: v for k, v in resolved.l1_facts.items() if src.get(k) == "l1"},
        "l2_recommended": {k: v for k, v in resolved.l2_approved.items() if src.get(k) == "l2"},
    }


# Backward-compat alias — prefer build_parameter_sources_from_state for new code.
build_parameter_sources = build_parameter_sources_from_state



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
