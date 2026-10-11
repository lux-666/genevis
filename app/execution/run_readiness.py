"""Read-only readiness checks for one confirmed run before input freeze."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.execution.database_resolution import (
    DatabaseResolutionError,
    validate_review_databases,
)
from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    package_preflight,
    preflight,
)
from app.llm.recommender import OutputMethod, StructuredRecommender
from app.schemas.run_config import RunConfig


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
        "gapcloser": tuple(registry.by_stage.get("gapcloser", ())),
        "base_polish": tuple(registry.by_stage.get("base_polish", ())),
        "t2t": tuple(registry.by_stage.get("t2t", ())),
    }


def assembly_candidate_method_scope() -> tuple[str, ...]:
    """Return every registry Assembly candidate available for deferred runs.

    This is a dependency-capture scope, not an execution or readiness scope.
    In particular, callers must not interpret it as consent to preflight or
    execute every candidate.
    """

    registry = load_descriptor_registry()
    return tuple(registry.by_stage.get("assembly", ()))


_NON_EXECUTABLE_METHODS = {"read_qc.length_filter.skip"}
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
            if method_name == "assembly_dedup.eval.quast" and not (
                run_config.assembly_dedup
                and run_config.assembly_dedup.recommendation_mode == "advisor"
                and run_config.assembly_dedup.automatic_purge_approved
            ):
                continue
            if method_name in {
                "gapcloser.eval.quast",
                "base_polish.eval.quast",
                "t2t.eval.quast",
            }:
                config = getattr(run_config, stage)
                if (
                    config is not None
                    and config.recommendation_mode == "deterministic_baseline"
                ):
                    continue
            add(stage, method_name)
        if stage == "read_qc":
            length_filter = (
                run_config.read_qc.length_filter
                if run_config.read_qc is not None
                else None
            )
            if length_filter is not None and length_filter.approved:
                add(stage, length_filter.selected_method())
        elif stage == "assembly":
            # Assembly candidates are captured at freeze time, but none is
            # startup-preflighted as the selected method before recommendation.
            pass

        if stage not in fixed_local_methods and stage not in {"read_qc", "assembly"}:
            for method_name in explicitly_selected.get(stage, ()):
                add(stage, method_name)
    return {stage: tuple(methods) for stage, methods in closure.items()}


def run_requires_llm(run_config: RunConfig) -> bool:
    """Return whether the confirmed workflow reaches a mandatory LLM boundary."""

    return bool(required_llm_output_methods(run_config))


def required_llm_output_methods(run_config: RunConfig) -> tuple[OutputMethod, ...]:
    """Select transport protocols from the same frozen advisor choices."""
    methods: list[OutputMethod] = []
    for stage in run_config.requested_stage_names():
        config = getattr(run_config, stage, None)
        if stage in {"assembly", "hic_scaffolding"}:
            if stage == "assembly":
                enabled = config is None or (
                    config.method_route_advisor == "advisor"
                    or config.parameter_advisor == "advisor"
                )
            else:
                enabled = config is None or config.parameter_advisor == "advisor"
            if enabled:
                methods.append("prompt_json")
        elif stage in {"assembly_dedup", "gapcloser", "t2t", "base_polish"}:
            if stage == "assembly_dedup" and (
                config is None or not config.automatic_purge_approved
            ):
                continue
            if config is None or config.recommendation_mode == "advisor":
                methods.append(
                    config.recommendation_output_method if config else "json_schema"
                )
                # T2T motif resolution is a separate prompt-JSON call only
                # when no motif was supplied in the frozen shared facts.
                if stage == "t2t" and not _t2t_motif_is_supplied(run_config):
                    methods.append("prompt_json")
    return tuple(dict.fromkeys(methods))


def _t2t_motif_is_supplied(run_config: RunConfig) -> bool:
    """Return whether T2T can skip its optional motif resolver model."""

    shared = run_config.shared_parameters
    facts = shared.species_facts if shared is not None else None
    return facts is not None and facts.telomere_motif is not None


def preflight_run_readiness(
    run_config: RunConfig,
    *,
    effective_methods: Mapping[str, str | Sequence[str]],
    species_name: str | None = None,
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
    try:
        review_database_projection = validate_review_databases(
            run_config,
            resolver,
            species_name=species_name,
        )
    except DatabaseResolutionError as exc:
        raise RunReadinessError(f"database readiness failed: {exc}") from exc
    for method_name in methods:
        stage = method_name.split(".", 1)[0]
        try:
            spec = resolver.resolve(stage=stage, method_name=method_name)
            packages = resolver.packages_for_spec(spec)
        except (TypeError, ValueError) as exc:
            failures.append(f"{method_name}: invalid dependency configuration: {exc}")
            continue
        if not spec.required_tools and not spec.package_tools:
            failures.append(
                f"{method_name}: resolved env mapping declares no required_tools "
                "or package_tools"
            )
            continue
        try:
            result = preflight(spec, runner=runner)
        except OSError as exc:
            failures.append(f"{method_name}: environment probe failed: {exc}")
            continue
        if result.status != "ok":
            failures.append(f"{method_name}: {result.detail or result.status}")
            continue
        if method_name == _BUSCO_METHOD:
            version_failure = _busco_version_failure(
                run_config,
                spec=spec,
                runner=runner,
                lineage_version=(
                    str(
                        review_database_projection.get("busco_parameters", {}).get(
                            "lineage_version", ""
                        )
                    )
                    or None
                ),
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
        raise RunReadinessError("local method readiness failed: " + "; ".join(failures))

    if run_requires_llm(run_config):
        try:
            if llm_probe is not None:
                llm_probe()
            else:
                for method in required_llm_output_methods(run_config):
                    probe_llm_endpoint(method)
        except Exception as exc:
            raise RunReadinessError(f"mandatory LLM readiness failed: {exc}") from exc
    return methods


def _busco_version_failure(
    run_config: RunConfig,
    *,
    spec: EnvSpec,
    runner: Callable[..., Any] | None,
    lineage_version: str | None = None,
) -> str | None:
    """Return a run-specific BUSCO compatibility failure, if any."""

    from app.configuration import load_descriptor_registry

    descriptor = load_descriptor_registry().packages["busco"]
    selector = f"lineage_version={lineage_version or 'unknown'}"
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
        return f"BUSCO {found} does not satisfy >={expected} required for {selector}"
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


def probe_llm_endpoint(method: OutputMethod = "prompt_json") -> None:
    """Prove the configured model can return the structured output we consume."""

    from app.llm.factory import build_chat_model

    def parse(payload: dict[str, Any]) -> dict[str, Any]:
        if payload != {"ready": True}:
            raise ValueError("LLM endpoint failed the structured-output probe")
        return payload

    result = StructuredRecommender(
        parse,
        method=method,
        schema={
            "type": "object",
            "properties": {"ready": {"type": "boolean", "enum": [True]}},
            "required": ["ready"],
            "additionalProperties": False,
        },
    ).invoke(
        [
            (
                "system",
                'This is a structured-output probe. Reply exactly {"ready":true}.',
            ),
            ("human", "Return the requested JSON object."),
        ],
        model_factory=build_chat_model,
    )
    if not result.valid:
        raise ValueError(f"LLM {method} readiness probe failed: {result.error}")


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
