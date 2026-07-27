"""Bootstrap-time preflight for every configured method environment."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    PreflightResult,
    PreflightStatus,
    preflight,
)


@dataclass
class MethodPreflightResult:
    """Detailed outcome for one configured method."""

    method_name: str
    stage: str
    conda_bin: str
    env_target: str
    status: PreflightStatus
    required_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    package_tools: list[str] = field(default_factory=list)
    detail: str = ""


def preflight_methods(
    resolver: EnvResolver,
    *,
    runner: Callable[..., Any] | None = None,
) -> list[MethodPreflightResult]:
    """Probe every configured method, grouping identical conda targets.

    The existing :func:`preflight` function remains the single probe contract.
    Grouping only reduces duplicate environment/tool probes; the returned list
    still has one result per method so operators can fix the exact config entry.
    """
    grouped: OrderedDict[tuple[str, str | None, str | None], dict[str, Any]] = (
        OrderedDict()
    )
    method_specs = resolver.configured_method_specs()

    for method_name, spec in method_specs:
        key = (spec.conda_bin, spec.conda_env, spec.conda_prefix)
        group = grouped.setdefault(
            key,
            {
                "spec": spec,
                "methods": [],
                "tools": OrderedDict(),
            },
        )
        group["methods"].append((method_name, spec))
        for tool in spec.required_tools:
            group["tools"].setdefault(tool, None)

    results_by_method: dict[str, MethodPreflightResult] = {}
    for group in grouped.values():
        base_spec: EnvSpec = group["spec"]
        grouped_spec = base_spec.model_copy(
            update={"required_tools": list(group["tools"].keys())}
        )
        try:
            group_result = preflight(grouped_spec, runner=runner)
        except OSError as exc:
            group_result = PreflightResult(
                status="blocked_env",
                env_target=base_spec.display_target(),
                detail=(
                    f"unable to execute conda probe for {base_spec.display_target()}: "
                    f"{exc}"
                ),
            )

        missing = set(group_result.missing_tools)
        for method_name, spec in group["methods"]:
            method_missing = [tool for tool in spec.required_tools if tool in missing]
            if group_result.status == "blocked_env":
                status: PreflightStatus = "blocked_env"
                detail = group_result.detail
            elif method_missing:
                status = "missing_tool"
                detail = (
                    f"tools missing in {spec.display_target()}: "
                    f"{', '.join(method_missing)}"
                )
            else:
                status = "ok"
                detail = ""
            results_by_method[method_name] = MethodPreflightResult(
                method_name=method_name,
                stage=method_name.split(".", 1)[0],
                conda_bin=spec.conda_bin,
                env_target=spec.display_target(),
                status=status,
                required_tools=list(spec.required_tools),
                missing_tools=method_missing,
                package_tools=list(spec.package_tools),
                detail=detail,
            )
    return [results_by_method[method_name] for method_name, _ in method_specs]
