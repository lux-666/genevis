"""Stage-level helper to dispatch a method attempt through its conda env.

Wraps :class:`~app.execution.env_resolver.EnvResolver` and
:func:`~app.execution.env_resolver.preflight` so every stage that freezes a
local attempt shares one resolution path: decide whether the caller has opted
out (PATH override), resolve the env, preflight, and either return the spec
the stage should attach to its :class:`FrozenAttemptSpec` or a structured
blocked payload plus a durable `env_preflight_blocked` ledger event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.env_resolver import (
    DEFAULT_CONFIG_PATH,
    EnvResolver,
    EnvSpec,
    preflight,
)


def has_explicit_path_override(stage_out: dict[str, Any]) -> bool:
    """True when the caller already supplied a PATH env override.

    Existing fake-binary stage tests inject PATH into
    ``stage_outputs[<stage>].env`` to make the planned command run against a
    stubbed bin directory. When that happens this dispatcher steps aside so
    the legacy shell execution path in :class:`LocalRunner` stays intact.
    """
    env = stage_out.get("env") or {}
    return isinstance(env, dict) and bool(env.get("PATH"))


def resolve_method_env(
    stage_out: dict[str, Any],
    *,
    stage: str,
    method_name: str,
    session_id: str,
    data_dir: str | Path,
    input_manifest_ref: str,
    config_path: Path | None = None,
) -> tuple[EnvSpec | None, dict[str, Any] | None]:
    """Resolve and preflight the conda env for one method.

    Returns ``(spec, None)`` when the caller should attach ``spec``'s conda
    fields to its ``FrozenAttemptSpec``; ``(None, None)`` when the caller
    supplied an explicit PATH override and the dispatcher is stepping aside;
    and ``(None, payload)`` when preflight failed. ``payload`` has
    ``status`` / ``reason`` / ``env_target`` / ``missing_tools`` /
    ``method_name`` so callers can merge it into their stage output dict and
    route to the stage's terminal failure edge.
    """
    if has_explicit_path_override(stage_out):
        return None, None

    resolver = EnvResolver(config_path or DEFAULT_CONFIG_PATH)
    spec = resolver.resolve(stage=stage, method_name=method_name)
    result = preflight(spec)
    if result.status == "ok":
        return spec, None

    ledger = Ledger(Path(data_dir))
    ledger.create_session(session_id, input_manifest_ref)
    ledger.record_event(
        session_id,
        "env_preflight_blocked",
        {
            "stage": stage,
            "method_name": method_name,
            "status": result.status,
            "env_target": result.env_target,
            "missing_tools": result.missing_tools,
            "detail": result.detail,
        },
    )
    return None, {
        "status": result.status,
        "reason": result.detail,
        "env_target": result.env_target,
        "missing_tools": result.missing_tools,
        "method_name": method_name,
    }


def attach_conda_fields(
    spec: EnvSpec | None,
    attempt_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``spec``'s conda bin/env/prefix into a FrozenAttemptSpec kwargs dict."""
    if spec is None:
        return attempt_kwargs
    attempt_kwargs["conda_bin"] = spec.conda_bin
    attempt_kwargs["conda_env"] = spec.conda_env
    attempt_kwargs["conda_prefix"] = spec.conda_prefix
    return attempt_kwargs
