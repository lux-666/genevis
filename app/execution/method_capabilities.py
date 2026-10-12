"""Conditional executable capabilities for frozen method environments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.configuration import load_descriptor_registry
from app.execution.env_resolver import EnvSpec
from app.schemas.run_config import RunConfig

_TRIO_HIFIASM_METHOD = "assembly.candidate.hifiasm"


def trio_enabled(run_config: RunConfig) -> bool:
    """Return whether the confirmed run supplies both explicit Trio roles."""

    assembly = run_config.assembly
    return bool(
        assembly and assembly.hifiasm_inputs and assembly.hifiasm_inputs.trio_enabled
    )


def conditional_required_tools(
    run_config: RunConfig,
    method_name: str,
) -> tuple[str, ...]:
    """Return descriptor-declared tools activated by this run's capabilities."""

    if method_name != _TRIO_HIFIASM_METHOD or not trio_enabled(run_config):
        return ()
    registry = load_descriptor_registry()
    descriptor = registry.methods[method_name]
    package = registry.packages[descriptor.package_ref]
    return tuple(package.conditional_required_tools.get("trio", ()))


def augment_method_env_spec(
    spec: EnvSpec,
    *,
    run_config: RunConfig,
    method_name: str,
) -> EnvSpec:
    """Add conditional tools to the immutable method environment contract."""

    additions = conditional_required_tools(run_config, method_name)
    if not additions:
        return spec
    required_tools = list(dict.fromkeys([*spec.required_tools, *additions]))
    return spec.model_copy(update={"required_tools": required_tools})


def required_tools_by_method(run_config: RunConfig) -> Mapping[str, Sequence[str]]:
    """Return conditional tool additions needed in a dependency snapshot."""

    additions = conditional_required_tools(run_config, _TRIO_HIFIASM_METHOD)
    return {_TRIO_HIFIASM_METHOD: additions} if additions else {}
