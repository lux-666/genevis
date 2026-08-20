"""Read-only readiness checks for one confirmed run before input freeze."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    package_preflight,
    preflight,
)
from app.schemas.run_config import RunConfig, infer_assembly_method


class RunReadinessError(ValueError):
    """Raised when the confirmed run cannot start on the current machine."""


def _fixed_local_methods() -> dict[str, tuple[str, ...]]:
    """Return registry-derived rosters for stages with fixed local methods."""

    registry = load_descriptor_registry()
    read_qc = tuple(
        method_name
        for method_name in registry.by_stage.get("read_qc", ())
        if registry.methods[method_name].planner.selection_role == "always"
    )
    return {
        "read_qc": read_qc,
        "genome_survey": tuple(registry.by_stage.get("genome_survey", ())),
        "assembly_dedup": tuple(registry.by_stage.get("assembly_dedup", ())),
        "assembly_review": tuple(registry.by_stage.get("assembly_review", ())),
        "hic_scaffolding": tuple(registry.by_stage.get("hic_scaffolding", ())),
        "t2t": tuple(registry.by_stage.get("t2t", ())),
    }

_NON_EXECUTABLE_METHODS = {"read_qc.length_filter.skip"}
_LLM_REQUIRED_STAGES = {"assembly", "hic_scaffolding", "t2t"}
_BUSCO_METHOD = "assembly_review.eval.busco"


def effective_method_closure(
    run_config: RunConfig,
    *,
    selected_methods: Mapping[str, str | Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return the canonical local-method closure for readiness and freeze."""

    fixed_local_methods = _fixed_local_methods()
    local_stages = list(
        dict.fromkeys(
            request.stage
            for request in run_config.requested_stages
            if request.allow_local_execution
        )
    )
    explicitly_selected = _normalize_selected_methods(selected_methods)
    closure: dict[str, list[str]] = {}

    def add(stage: str, method_name: str) -> None:
        if method_name in _NON_EXECUTABLE_METHODS:
            return
        methods = closure.setdefault(stage, [])
        if method_name not in methods:
            methods.append(method_name)

    for stage in local_stages:
        for method_name in fixed_local_methods.get(stage, ()):
            add(stage, method_name)
        if stage == "read_qc":
            length_filter = (
                run_config.read_qc.length_filter
                if run_config.read_qc is not None
                else None
            )
            if (
                length_filter is not None
                and length_filter.approved
                and length_filter.method != "skip"
            ):
                add(stage, length_filter.method)
        elif stage == "assembly":
            method = (
                infer_assembly_method(run_config.assembly)
                if run_config.assembly
                else None
            )
            if method is None:
                raise RunReadinessError(
                    "local Assembly readiness requires one selected assembler"
                )
            add(stage, f"assembly.candidate.{method}")

        if stage not in fixed_local_methods and stage not in {"read_qc", "assembly"}:
            for method_name in explicitly_selected.get(stage, ()):
                add(stage, method_name)
    return {stage: tuple(methods) for stage, methods in closure.items()}


def selected_local_method_closure(
    run_config: RunConfig,
    *,
    selected_methods: Mapping[str, str | Sequence[str]] | None = None,
) -> list[str]:
    """Flatten :func:`effective_method_closure` for diagnostics and callers."""

    closure = effective_method_closure(
        run_config,
        selected_methods=selected_methods,
    )
    return [method for methods in closure.values() for method in methods]


def run_requires_llm(run_config: RunConfig) -> bool:
    """Return whether the confirmed workflow reaches a mandatory LLM boundary."""

    return bool(_LLM_REQUIRED_STAGES.intersection(run_config.requested_stage_names()))


def preflight_run_readiness(
    run_config: RunConfig,
    *,
    effective_methods: Mapping[str, str | Sequence[str]],
    config_path: str | Path | None = None,
    runner: Callable[..., Any] | None = None,
    llm_probe: Callable[[], None] | None = None,
) -> list[str]:
    """Probe only this run's local method closure and mandatory LLM endpoint."""

    methods = _flatten_effective_methods(effective_methods)
    failures: list[str] = []
    try:
        # The dependency snapshot loads the same configuration even for an
        # Inventory-only run. Validate it here before freeze creates the layout.
        resolver = EnvResolver(config_path)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise RunReadinessError(
            f"dependency configuration is not ready: {exc}"
        ) from exc
    for method_name in methods:
        stage = method_name.split(".", 1)[0]
        try:
            spec = resolver.resolve(stage=stage, method_name=method_name)
            packages = resolver.packages_for_spec(spec)
        except (TypeError, ValueError) as exc:
            failures.append(f"{method_name}: invalid dependency configuration: {exc}")
            continue
        try:
            result = preflight(spec, runner=runner)
        except OSError as exc:
            failures.append(f"{method_name}: environment probe failed: {exc}")
            continue
        if result.status != "ok":
            failures.append(
                f"{method_name}: {result.detail or result.status}"
            )
            continue
        if method_name == _BUSCO_METHOD:
            version_failure = _busco_version_failure(
                run_config,
                spec=spec,
                runner=runner,
            )
            if version_failure is not None:
                failures.append(f"{method_name}: {version_failure}")
                continue
        for package in packages:
            package_result = package_preflight(package)
            if package_result.status != "ok":
                missing = ", ".join(package_result.missing_paths)
                failures.append(
                    f"{method_name}: package {package.name} is not ready"
                    + (f": {missing}" if missing else "")
                )

    if failures:
        raise RunReadinessError(
            "local method readiness failed: " + "; ".join(failures)
        )

    if run_requires_llm(run_config):
        try:
            (llm_probe or probe_llm_endpoint)()
        except Exception as exc:
            raise RunReadinessError(f"mandatory LLM readiness failed: {exc}") from exc
    return methods


def _busco_version_failure(
    run_config: RunConfig,
    *,
    spec: EnvSpec,
    runner: Callable[..., Any] | None,
) -> str | None:
    """Return a run-specific BUSCO compatibility failure, if any."""

    from app.configuration import load_descriptor_registry

    review = run_config.assembly_review
    if review is None:
        return "assembly_review.busco configuration is missing"
    descriptor = load_descriptor_registry().packages["busco"]
    selector = f"lineage_version={review.busco.lineage_version}"
    constraints = [descriptor.version_constraint]
    conditional = descriptor.conditional_version_constraints.get(selector)
    if conditional is not None:
        constraints.append(conditional)
    try:
        minimum = max(_minimum_version(constraint) for constraint in constraints)
    except ValueError as exc:
        return f"invalid BUSCO descriptor version constraint: {exc}"

    run = runner if runner is not None else subprocess.run
    command = [*spec.conda_run_prefix(), "busco", "--version"]
    try:
        result = run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return f"BUSCO version probe failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return "BUSCO --version failed" + (f": {detail}" if detail else "")
    output = "\n".join(
        part for part in (result.stdout, result.stderr) if isinstance(part, str)
    )
    actual = _parse_busco_version(output)
    if actual is None:
        return f"BUSCO --version returned an unrecognized value: {output.strip()!r}"
    if _pad_version(actual, len(minimum)) < _pad_version(minimum, len(actual)):
        expected = ".".join(str(part) for part in minimum)
        found = ".".join(str(part) for part in actual)
        return (
            f"BUSCO {found} does not satisfy >={expected} required for {selector}"
        )
    return None


def _minimum_version(constraint: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*>=\s*(\d+(?:\.\d+)*)\s*", constraint)
    if match is None:
        raise ValueError(f"unsupported constraint {constraint!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _parse_busco_version(output: str) -> tuple[int, ...] | None:
    match = re.search(r"(?im)^\s*BUSCO\s+v?(\d+(?:\.\d+)+)\b", output)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _pad_version(version: tuple[int, ...], size: int) -> tuple[int, ...]:
    return version + (0,) * max(0, size - len(version))


def probe_llm_endpoint() -> None:
    """Perform a minimal authenticated model call to prove endpoint connectivity."""

    from app.llm.factory import build_chat_model
    from app.llm.json_response import response_text

    model = build_chat_model()
    response = model.invoke(
        [
            ("system", "This is a connectivity probe. Reply with READY."),
            ("human", "READY"),
        ]
    )
    if not response_text(response).strip():
        raise ValueError("LLM endpoint returned an empty response")


def _normalize_selected_methods(
    selected_methods: Mapping[str, str | Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_stage, raw_methods in (selected_methods or {}).items():
        stage = str(raw_stage).strip()
        values = [raw_methods] if isinstance(raw_methods, str) else list(raw_methods)
        methods = tuple(str(value).strip() for value in values)
        if not stage or any(not method for method in methods):
            raise RunReadinessError("selected method scope contains an empty name")
        if any(not method.startswith(f"{stage}.") for method in methods):
            raise RunReadinessError(
                f"selected method does not belong to stage {stage!r}"
            )
        normalized[stage] = methods
    return normalized


def _flatten_effective_methods(
    effective_methods: Mapping[str, str | Sequence[str]],
) -> list[str]:
    normalized = _normalize_selected_methods(effective_methods)
    methods: list[str] = []
    for stage_methods in normalized.values():
        for method in stage_methods:
            if method not in _NON_EXECUTABLE_METHODS and method not in methods:
                methods.append(method)
    return methods
