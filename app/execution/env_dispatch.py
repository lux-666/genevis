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

from app.execution import env_resolver
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


def resolve_method_env(
    stage_out: Mapping[str, object],
    *,
    stage: str,
    method_name: str,
    session_id: str,
    data_dir: str | Path,
    input_manifest_ref: str,
    config_path: Path | None = None,
) -> tuple[EnvSpec | None, list[PackageSpec], MethodEnvBlock | None]:
    """Resolve and preflight the conda env + external packages for one method.

    Returns ``(spec, packages, None)`` when the caller should attach ``spec``'s
    conda fields and use ``packages`` (already preflighted) to build explicit
    script paths; ``(None, [], None)`` when the caller supplied an explicit
    PATH override on the conda side (packages still returned empty, since the
    fake-bin tests don't declare package_tools); and ``(None, [], payload)``
    when preflight failed. ``payload`` has ``status`` / ``reason`` /
    ``env_target`` / ``missing_tools`` / ``method_name`` so callers can merge
    it into their stage output dict and route to the terminal failure edge.
    """
    if has_explicit_path_override(stage_out):
        # PATH override: skip conda preflight, but still resolve and preflight
        # external packages (filesystem-only, independent of conda/PATH) so the
        # Juicer method can locate entry scripts from the declared roots. When
        # the env-config is absent (e.g. tests that drive the PATH-only bypass with
        # a PATH-only stage output) there are no packages to preflight, fall
        # through to the ``(None, [], None)`` PATH-only bypass.
        resolver = _frozen_or_live_resolver(data_dir=data_dir, config_path=config_path)
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
                            status="missing_tool",
                            env_target=package.install_root,
                            missing_tools=check.missing_paths,
                            detail=check.detail,
                        ),
                    )
            return None, packages, None
        return None, [], None

    resolver = _frozen_or_live_resolver(data_dir=data_dir, config_path=config_path)
    if resolver is None:
        # This branch is reachable only for the existing PATH-only test and
        # override behavior when no active config exists.  A frozen session
        # never reaches it: incomplete/tampered frozen contracts raise above.
        resolver = EnvResolver(config_path)
    spec = resolver.resolve(stage=stage, method_name=method_name)
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
                status="missing_tool",
                env_target=packages[0].install_root,
                missing_tools=all_missing,
                detail=detail,
            ),
        )

    return spec, packages, None


def _frozen_or_live_resolver(
    *,
    data_dir: str | Path,
    config_path: Path | None,
) -> _DependencyResolver | None:
    """Return the run snapshot resolver when one exists, else live config.

    New-format frozen sessions are intentionally strict: even an explicit
    ``config_path`` injection cannot replace the recorded machine dependency
    contract.  Legacy runs with no frozen-input signal retain their previous
    live-config behavior while the TUI migration is in progress.
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
    active = env_resolver.active_config_path()
    if active.exists():
        return EnvResolver(active)
    return None


def _record_preflight_block(
    *,
    data_dir: str | Path,
    session_id: str,
    input_manifest_ref: str,
    stage: str,
    method_name: str,
    status: str,
    env_target: str,
    missing_tools: list[str],
    detail: str,
) -> MethodEnvBlock:
    event = {
        "stage": stage,
        "method_name": method_name,
        "status": status,
        "env_target": env_target,
        "missing_tools": missing_tools,
        "detail": detail,
    }
    ledger = Ledger(Path(data_dir))
    ledger.create_session(session_id, input_manifest_ref)
    ledger.record_event(session_id, "env_preflight_blocked", event)
    return {
        "status": status,
        "reason": detail,
        "env_target": env_target,
        "missing_tools": missing_tools,
        "method_name": method_name,
    }
