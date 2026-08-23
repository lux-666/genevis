"""Stage-level helper to dispatch a method attempt through its conda env.

Wraps :class:`~app.execution.env_resolver.EnvResolver` and
:func:`~app.execution.env_resolver.preflight` so every stage that freezes a
local attempt shares one resolution path: decide whether the caller has opted
out (PATH override), resolve the env, preflight, and either return the spec
the stage should attach to its :class:`FrozenAttemptSpec` or a structured
blocked payload plus a durable `env_preflight_blocked` ledger event.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from typing_extensions import TypedDict

from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    PackagePreflightResult,
    PackageSpec,
    package_preflight,
    preflight,
)
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths


class MethodEnvBlock(TypedDict):
    """Structured stage update emitted when environment preflight blocks."""

    status: str
    reason: str
    env_target: str
    missing_tools: list[str]
    method_name: str


class _DependencyResolver(Protocol):
    def resolve(self, *, stage: str, method_name: str | None = None) -> EnvSpec: ...

    def packages_for_spec(self, spec: EnvSpec) -> list[PackageSpec]: ...


def has_explicit_path_override(stage_out: Mapping[str, object]) -> bool:
    """True when the caller already supplied a PATH env override.

    Existing fake-binary stage tests inject PATH into
    ``stage_outputs[<stage>].env`` to make the planned command run against a
    stubbed bin directory. When that happens this dispatcher steps aside on
    the conda side so the PATH-driven shell execution path in :class:`LocalRunner`
    stays intact. Package preflight still runs (it is filesystem-only and
    independent of conda/PATH).
    """
    env = stage_out.get("env") or {}
    return isinstance(env, Mapping) and bool(env.get("PATH"))


def _has_frozen_input_contract(data_dir: str | Path) -> bool:
    # A PATH-only execution is a pre-freeze/unit-test seam. Once a run has
    # committed control inputs, the stage cursor is untrusted and cannot opt
    # out of the frozen dependency resolver. Treat partial transactions as
    # frozen too so execution fails closed instead of using an injected PATH.
    try:
        paths = SessionPaths.from_run_dir(data_dir)
    except DataLayoutError:
        return False
    from app.storage.frozen_inputs import has_frozen_input_contract

    return has_frozen_input_contract(paths)


def resolve_method_env(
    stage_out: Mapping[str, object],
    *,
    stage: str,
    method_name: str,
    workflow_step_id: str,
    session_id: str,
    data_dir: str | Path,
    input_manifest_ref: str,
    config_path: Path | None = None,
) -> tuple[EnvSpec | None, list[PackageSpec], MethodEnvBlock | None]:
    """Resolve and preflight the conda env + external packages for one method.

    Returns ``(spec, packages, None)`` when the caller should attach ``spec``'s
    conda fields and use ``packages`` (already preflighted) to build explicit
    script paths; ``(None, [], None)`` when the caller supplied an explicit
    PATH override through the pre-freeze/unit-test seam; and ``(None, [],
    payload)`` when preflight failed. A PATH override is rejected once a
    frozen or partial transaction exists. ``payload`` has ``status`` /
    ``reason`` / ``env_target`` / ``missing_tools`` / ``method_name`` so
    callers can merge it into their stage output dict and route to the
    terminal failure edge.
    """
    if not isinstance(workflow_step_id, str) or not workflow_step_id:
        raise ValueError("workflow_step_id must be a non-empty string")
    if has_explicit_path_override(stage_out):
        if _has_frozen_input_contract(data_dir):
            return (
                None,
                [],
                _record_preflight_block(
                    data_dir=data_dir,
                    session_id=session_id,
                    input_manifest_ref=input_manifest_ref,
                    stage=stage,
                    method_name=method_name,
                    workflow_step_id=workflow_step_id,
                    status="blocked_env",
                    env_target="frozen-dependency-contract",
                    missing_tools=[],
                    detail=(
                        "explicit stage cursor PATH override is rejected after "
                        "the frozen input contract exists; use the frozen "
                        "dependency resolver"
                    ),
                ),
            )
        # PATH override: skip conda preflight, but still resolve and preflight
        # external packages (filesystem-only, independent of conda/PATH) so the
        # Juicer method can locate entry scripts from the declared roots. When
        # the env-config is absent (e.g. tests that drive the PATH-only bypass with
        # a PATH-only stage output) there are no packages to preflight, fall
        # through to the ``(None, [], None)`` PATH-only bypass.
        resolver = _dependency_resolver(data_dir=data_dir, config_path=config_path)
        if resolver is not None:
            spec = resolver.resolve(stage=stage, method_name=method_name)
            packages = resolver.packages_for_spec(spec)
            for package in packages:
                check = package_preflight(package)
                if check.status != "ok":
                    return (
                        None,
                        [],
                        _record_preflight_block(
                            data_dir=data_dir,
                            session_id=session_id,
                            input_manifest_ref=input_manifest_ref,
                            stage=stage,
                            method_name=method_name,
                            workflow_step_id=workflow_step_id,
                            status="missing_tool",
                            env_target=package.install_root,
                            missing_tools=check.missing_paths,
                            detail=check.detail,
                        ),
                    )
            return None, packages, None
        return None, [], None

    resolver = _dependency_resolver(data_dir=data_dir, config_path=config_path)
    if resolver is None:
        raise DataLayoutError(
            "method execution requires a frozen dependency contract or an "
            "explicit config_path"
        )
    spec = resolver.resolve(stage=stage, method_name=method_name)
    if not spec.required_tools and not spec.package_tools:
        detail = (
            f"{method_name}: resolved frozen env mapping declares no "
            "required_tools or package_tools"
        )
        return (
            None,
            [],
            _record_preflight_block(
                data_dir=data_dir,
                session_id=session_id,
                input_manifest_ref=input_manifest_ref,
                stage=stage,
                method_name=method_name,
                workflow_step_id=workflow_step_id,
                status="missing_tool",
                env_target=spec.display_target(),
                missing_tools=[],
                detail=detail,
            ),
        )
    conda_result = preflight(spec)
    if conda_result.status != "ok":
        return (
            None,
            [],
            _record_preflight_block(
                data_dir=data_dir,
                session_id=session_id,
                input_manifest_ref=input_manifest_ref,
                stage=stage,
                method_name=method_name,
                workflow_step_id=workflow_step_id,
                status=conda_result.status,
                env_target=conda_result.env_target,
                missing_tools=conda_result.missing_tools,
                detail=conda_result.detail,
            ),
        )

    packages = resolver.packages_for_spec(spec)
    missing_by_package: list[tuple[str, PackagePreflightResult]] = []
    for package in packages:
        result = package_preflight(package)
        if result.status != "ok":
            missing_by_package.append((package.name, result))

    if missing_by_package:
        all_missing = [
            rel for _, result in missing_by_package for rel in result.missing_paths
        ]
        detail = "; ".join(
            f"{name}: {result.detail}" for name, result in missing_by_package
        )
        return (
            None,
            [],
            _record_preflight_block(
                data_dir=data_dir,
                session_id=session_id,
                input_manifest_ref=input_manifest_ref,
                stage=stage,
                method_name=method_name,
                workflow_step_id=workflow_step_id,
                status="missing_tool",
                env_target=packages[0].install_root,
                missing_tools=all_missing,
                detail=detail,
            ),
        )

    return spec, packages, None


def _dependency_resolver(
    *,
    data_dir: str | Path,
    config_path: Path | None,
) -> _DependencyResolver | None:
    """Return the frozen resolver, or an explicitly injected config resolver.

    A committed frozen session always owns dependency resolution. ``config_path``
    remains only as an explicit pre-freeze/test seam and cannot replace a
    recorded snapshot. There is no implicit active-config fallback.
    """

    try:
        paths = SessionPaths.from_run_dir(data_dir)
    except DataLayoutError:
        paths = None
    if paths is not None:
        from app.storage.frozen_inputs import has_frozen_input_contract

        if has_frozen_input_contract(paths):
            from app.execution.dependency_snapshot import (
                load_frozen_dependency_resolver,
            )

            return load_frozen_dependency_resolver(paths.run_dir)

    if config_path is not None:
        return EnvResolver(config_path)
    return None


def _record_preflight_block(
    *,
    data_dir: str | Path,
    session_id: str,
    input_manifest_ref: str,
    stage: str,
    method_name: str,
    workflow_step_id: str,
    status: str,
    env_target: str,
    missing_tools: list[str],
    detail: str,
) -> MethodEnvBlock:
    event = {
        "method_name": method_name,
        "status": status,
        "env_target": env_target,
        "missing_tools": missing_tools,
        "detail": detail,
    }
    ledger = Ledger(Path(data_dir))
    ledger.create_session(session_id, input_manifest_ref)
    ledger.record_occurrence_event(
        session_id,
        "env_preflight_blocked",
        event,
        stage=stage,
        workflow_step_id=workflow_step_id,
        subject_id=f"{method_name}:{env_target}",
        idempotency_key=(
            f"env-preflight-blocked:{workflow_step_id}:{method_name}:{env_target}"
        ),
    )
    return {
        "status": status,
        "reason": detail,
        "env_target": env_target,
        "missing_tools": missing_tools,
        "method_name": method_name,
    }
