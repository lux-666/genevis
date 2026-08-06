"""Immutable snapshots of machine-level execution dependencies.

The active ``config/envs.yaml`` is intentionally mutable machine state.  A
workflow run must not consult it after its inputs have been confirmed, so the
freeze transaction stores the resolved environment/package contract in
``control/dependency_manifest.json``.  This module owns the portable snapshot
shape and a resolver that uses only that stored contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    PackagePreflightResult,
    PackageSpec,
    PreflightResult,
    active_config_path,
    package_preflight,
    preflight,
)
from app.integrity import sha256_file
from app.storage.paths import SessionPaths

DEPENDENCY_MANIFEST_SCHEMA_VERSION = 2


class DependencySnapshotError(ValueError):
    """Raised when a dependency snapshot is malformed or cannot be trusted."""


class DependencyConfigIdentity(BaseModel):
    """Identity of the active machine configuration captured at freeze time."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class DependencySnapshot(BaseModel):
    """Serializable, secret-free machine dependency contract for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = DEPENDENCY_MANIFEST_SCHEMA_VERSION
    captured_at: str
    config: DependencyConfigIdentity
    selected_methods: list[str] = Field(default_factory=list)
    default_environment: EnvSpec
    stage_environments: dict[str, EnvSpec] = Field(default_factory=dict)
    method_environments: dict[str, EnvSpec] = Field(default_factory=dict)
    packages: dict[str, PackageSpec] = Field(default_factory=dict)
    environment_preflight: dict[str, PreflightResult] = Field(default_factory=dict)
    package_preflight: dict[str, PackagePreflightResult] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != DEPENDENCY_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "unsupported dependency manifest schema "
                f"{value!r}; expected {DEPENDENCY_MANIFEST_SCHEMA_VERSION}"
            )
        return value


class FrozenDependencyResolver:
    """Resolve env/package dependencies solely from a frozen snapshot.

    The surface deliberately mirrors the two ``EnvResolver`` methods used by
    ``env_dispatch``.  It has no reference to an active config path and never
    reloads ``config/envs.yaml``.
    """

    def __init__(self, snapshot: DependencySnapshot) -> None:
        self.snapshot = snapshot
        self.config_path = Path(snapshot.config.source_path)
        self.packages = dict(snapshot.packages)

    def resolve(self, *, stage: str, method_name: str | None = None) -> EnvSpec:
        if method_name and method_name in self.snapshot.method_environments:
            return self.snapshot.method_environments[method_name]
        if stage in self.snapshot.stage_environments:
            return self.snapshot.stage_environments[stage]
        return self.snapshot.default_environment

    def packages_for_spec(self, spec: EnvSpec) -> list[PackageSpec]:
        missing = [name for name in spec.package_tools if name not in self.packages]
        if missing:
            raise DependencySnapshotError(
                "frozen env spec references unavailable package_tools: "
                + ", ".join(missing)
            )
        return [self.packages[name] for name in spec.package_tools]


def capture_dependency_snapshot(
    *,
    config_path: str | Path | None = None,
    stages: Iterable[str] | None = None,
    methods: Iterable[str] | None = None,
    preflight_runner: Callable[..., Any] | None = None,
    probe_environments: bool | None = None,
) -> DependencySnapshot:
    """Capture resolved environments and packages from active machine config.

    This reads only ``envs.yaml`` and declared package paths.  It never reads a
    ``.env`` file and serializes only typed environment/package fields, so
    secrets cannot enter the resulting manifest. With no explicit scope, every
    configured stage and method is captured for compatibility. A freeze caller
    supplies the confirmed scope; unconfigured names correctly use the frozen
    default instead of creating a synthetic override.

    Package filesystem outcomes are recorded for audit.  Conda environment
    probes are opt-in via ``probe_environments``; the TUI freeze path must not
    unexpectedly execute conda commands or turn sample intake into a machine
    readiness check.  A non-``ok`` result never causes a fallback to live
    configuration; the frozen executor repeats its normal probe against the
    stored specification.
    """

    source = Path(config_path or active_config_path()).expanduser().resolve(
        strict=False
    )
    try:
        resolver = EnvResolver(source)
        raw = resolver._raw
        selected_stage_scope = (
            list(dict.fromkeys(str(stage).strip() for stage in stages))
            if stages is not None
            else None
        )
        selected_method_scope = (
            sorted(dict.fromkeys(str(method).strip() for method in methods))
            if methods is not None
            else None
        )
        if selected_stage_scope is not None and any(
            not stage for stage in selected_stage_scope
        ):
            raise DependencySnapshotError(
                "dependency stage scope contains an empty name"
            )
        if selected_method_scope is not None and any(
            not method for method in selected_method_scope
        ):
            raise DependencySnapshotError(
                "dependency method scope contains an empty name"
            )
        stage_names = _snapshot_names(raw.get("stages"), selected_stage_scope)
        method_names = _snapshot_method_names(
            raw.get("methods"),
            requested=selected_method_scope,
            stage_scope=selected_stage_scope,
        )
        default_environment = resolver.resolve(stage="__frozen_default__")
        stage_environments = {
            stage: resolver.resolve(stage=stage) for stage in stage_names
        }
        method_environments = {
            method: resolver.resolve(
                stage=_stage_for_method(method, stage_names), method_name=method
            )
            for method in method_names
        }
    except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
        raise DependencySnapshotError(
            f"could not capture dependency configuration from {source}: {exc}"
        ) from exc

    all_specs = [
        ("default", default_environment),
        *[(f"stage:{name}", spec) for name, spec in stage_environments.items()],
        *[(f"method:{name}", spec) for name, spec in method_environments.items()],
    ]
    should_probe_environments = (
        preflight_runner is not None
        if probe_environments is None
        else probe_environments
    )
    environment_preflight = (
        _capture_environment_preflight(all_specs, runner=preflight_runner)
        if should_probe_environments
        else {}
    )
    package_names = sorted(
        {
            package.name
            for _spec_name, spec in all_specs
            for package in resolver.packages_for_spec(spec)
        }
    )
    selected_packages = {name: resolver.packages[name] for name in package_names}
    package_results = {
        name: package_preflight(package)
        for name, package in selected_packages.items()
    }
    return DependencySnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        config=DependencyConfigIdentity(
            source_path=str(source), sha256=sha256_file(source)
        ),
        selected_methods=selected_method_scope or [],
        default_environment=default_environment,
        stage_environments=stage_environments,
        method_environments=method_environments,
        packages=selected_packages,
        environment_preflight=environment_preflight,
        package_preflight=package_results,
    )


def dependency_snapshot_bytes(snapshot: DependencySnapshot) -> bytes:
    """Return the canonical JSON bytes stored by the freeze transaction."""

    return (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def load_dependency_snapshot(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> DependencySnapshot:
    """Load a dependency snapshot without consulting the active configuration."""

    snapshot_path = Path(path)
    try:
        raw_bytes = snapshot_path.read_bytes()
    except OSError as exc:
        raise DependencySnapshotError(
            f"dependency manifest is not readable: {snapshot_path}: {exc}"
        ) from exc
    actual_sha256 = _sha256_bytes(raw_bytes)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise DependencySnapshotError(
            "dependency manifest hash does not match frozen input marker: "
            f"{snapshot_path}"
        )
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise DependencySnapshotError(
            f"dependency manifest is not valid JSON: {snapshot_path}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise DependencySnapshotError("dependency manifest root must be an object")
    try:
        return DependencySnapshot.model_validate(raw)
    except ValidationError as exc:
        raise DependencySnapshotError(
            f"dependency manifest failed validation: {exc}"
        ) from exc


def load_frozen_dependency_resolver(
    data_dir: str | Path,
) -> FrozenDependencyResolver:
    """Return a resolver after verifying the run's frozen input commit marker."""

    paths = SessionPaths.from_run_dir(data_dir)
    # Import lazily to keep the freeze transaction's storage/execution boundary
    # acyclic at module import time.
    from app.storage.frozen_inputs import validate_frozen_inputs

    frozen = validate_frozen_inputs(paths)
    expected = frozen.file_sha256("dependency_manifest")
    snapshot = load_dependency_snapshot(
        paths.dependency_manifest_path,
        expected_sha256=expected,
    )
    return FrozenDependencyResolver(snapshot)


def _snapshot_names(
    raw_value: object,
    requested: Iterable[str] | None,
) -> list[str]:
    if raw_value is None:
        available: set[str] = set()
    elif isinstance(raw_value, Mapping):
        available = {str(name) for name in raw_value}
    else:
        raise DependencySnapshotError("config section must be a mapping")
    if requested is None:
        return sorted(available)
    return sorted(available.intersection(str(value) for value in requested))


def _snapshot_method_names(
    raw_value: object,
    *,
    requested: Iterable[str] | None,
    stage_scope: list[str] | None,
) -> list[str]:
    if requested is None:
        return _snapshot_names(raw_value, None)
    if raw_value is None:
        available: set[str] = set()
    elif isinstance(raw_value, Mapping):
        available = {str(name) for name in raw_value}
    else:
        raise DependencySnapshotError("config methods must be a mapping")

    requested_names = {str(value) for value in requested}
    closure = available.intersection(requested_names)
    if stage_scope:
        from app.configuration import load_descriptor_registry

        registry = load_descriptor_registry()
        planner_methods = {
            method
            for stage in stage_scope
            for method in (registry.by_stage.get(stage) or [])
        }
        explicitly_scoped_stages = {
            method.split(".", 1)[0] for method in requested_names
        }
        closure.update(
            method
            for method in available
            if method.split(".", 1)[0] in stage_scope
            and (
                method.split(".", 1)[0] not in explicitly_scoped_stages
                or method not in planner_methods
            )
        )
    return sorted(closure)


def _stage_for_method(method_name: str, configured_stages: list[str]) -> str:
    """Choose a stable stage fallback for a method-only config entry.

    Most method ids are ``<stage>.<family>.<name>``.  When that convention does
    not identify a configured stage, ``EnvResolver`` will correctly fall back to
    the default after consulting the method override, so a synthetic stage is
    sufficient and avoids filename/path guessing.
    """

    candidate = method_name.split(".", 1)[0]
    return candidate if candidate in configured_stages else "__frozen_method__"


def _capture_environment_preflight(
    specs: Iterable[tuple[str, EnvSpec]],
    *,
    runner: Callable[..., Any] | None,
) -> dict[str, PreflightResult]:
    cached: dict[str, PreflightResult] = {}
    result: dict[str, PreflightResult] = {}
    for name, spec in specs:
        identity = json.dumps(spec.model_dump(mode="json"), sort_keys=True)
        if identity not in cached:
            cached[identity] = preflight(spec, runner=runner)
        result[name] = cached[identity]
    return result


def _sha256_bytes(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()
