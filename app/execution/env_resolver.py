"""Stage- and method-specific conda environment resolution for local execution.

The resolver loads ``config/envs.yaml`` (the single source of truth — there is
no local-override file), answers "what conda env should method X run in", and
runs the small preflight probes (env exists, required tool inside the env).
External software packages (Juicer, 3D-DNA) live in the same file under a
top-level ``packages:`` section and are validated by filesystem checks via
:func:`package_preflight`. Call sites pass the results into
:class:`app.execution.local_runner.LocalRunner`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


DEFAULT_CONFIG_PATH = Path("config/envs.yaml")


def active_config_path() -> Path:
    """Return the single active env-config path.

    Honors ``GENE_AGENT_ENVS_CONFIG`` when set (absolute or repo-relative) so
    operators can point at a host-specific mapping and tests can point at a
    tmp fixture without threading ``config_path`` through every call site.
    """
    override = os.environ.get("GENE_AGENT_ENVS_CONFIG")
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH

PreflightStatus = Literal["ok", "blocked_env", "missing_tool"]


class EnvSpec(BaseModel):
    """Resolved conda environment for one method or stage."""

    conda_bin: str = "conda"
    conda_env: str | None = None
    conda_prefix: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    package_tools: list[str] = Field(default_factory=list)
    source: Literal["method", "stage", "default"] = "default"

    @model_validator(mode="after")
    def _at_most_one_target(self) -> "EnvSpec":
        if self.conda_env and self.conda_prefix:
            raise ValueError("conda_env and conda_prefix are mutually exclusive")
        if not self.conda_env and not self.conda_prefix:
            raise ValueError("env spec requires conda_env or conda_prefix")
        return self

    def conda_run_prefix(self) -> list[str]:
        """Return the `conda run -n NAME` / `-p PATH` argv prefix."""
        target = ["-p", self.conda_prefix] if self.conda_prefix else ["-n", self.conda_env]
        return [self.conda_bin, "run", "--live-stream", "--no-capture-output", *target]

    def display_target(self) -> str:
        return self.conda_prefix or self.conda_env or ""


class PreflightResult(BaseModel):
    """Structured preflight outcome for one resolved env spec."""

    status: PreflightStatus
    env_target: str
    missing_tools: list[str] = Field(default_factory=list)
    detail: str = ""


def _norm_rel(value: object) -> str:
    """Normalize a config canary/entry path to a root-relative POSIX string."""
    text = str(value).replace("\\", "/")
    return text.lstrip("/")


class PackageSpec(BaseModel):
    """An external software package install (Juicer, 3D-DNA) declared in envs.yaml.

    Unlike conda-env PATH tools, these are standalone git-clone installs whose
    layout is validated by filesystem checks against ``install_root`` +
    ``canary_paths``. ``conform_on_bootstrap`` marks packages whose raw repo
    layout must be conformed (symlinks built) before juicer.sh can run; that
    write step lives in :mod:`app.execution.package_bootstrap`, not here.
    """

    name: str
    install_root: str
    canary_paths: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    conform_on_bootstrap: bool = False
    jar_root_ref: str | None = None

    @model_validator(mode="after")
    def _normalize_paths(self) -> "PackageSpec":
        self.canary_paths = [_norm_rel(p) for p in self.canary_paths]
        self.entrypoints = [_norm_rel(p) for p in self.entrypoints]
        return self

    def resolved_canary(self, rel: str) -> Path:
        return Path(self.install_root) / rel


class PackagePreflightResult(BaseModel):
    """Outcome of validating one package's filesystem layout."""

    status: Literal["ok", "missing_tool"]
    package: str
    install_root: str
    missing_paths: list[str] = Field(default_factory=list)
    detail: str = ""


def package_preflight(package: PackageSpec) -> PackagePreflightResult:
    """Filesystem-only validation of one package's root + canary paths.

    Reads only — no PATH, no conda, no writes. A missing install_root or any
    missing canary file yields ``missing_tool`` with the offending relative
    paths in ``missing_paths``.
    """
    root = Path(package.install_root)
    missing: list[str] = []
    if not root.is_dir():
        return PackagePreflightResult(
            status="missing_tool",
            package=package.name,
            install_root=package.install_root,
            missing_paths=["<install_root>"],
            detail=f"install_root does not exist or is not a directory: {package.install_root}",
        )
    for rel in package.canary_paths:
        if not package.resolved_canary(rel).is_file():
            missing.append(rel)
    if missing:
        return PackagePreflightResult(
            status="missing_tool",
            package=package.name,
            install_root=package.install_root,
            missing_paths=missing,
            detail=f"missing in {package.name}: {', '.join(missing)}",
        )
    return PackagePreflightResult(
        status="ok",
        package=package.name,
        install_root=package.install_root,
    )


def _load_raw(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"env config missing: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"env config must be a mapping: {config_path}")
    return raw


def _entry(raw: dict, keys: tuple[str, ...]) -> dict | None:
    node: object = raw
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


class EnvResolver:
    """Resolve conda environments for stages and methods from the active config."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else active_config_path()
        self._raw = _load_raw(self.config_path)
        default_section = self._raw.get("default") or {}
        self.conda_bin: str = str(default_section.get("conda_bin") or "conda")
        fallback_env = default_section.get("fallback_env")
        fallback_prefix = default_section.get("fallback_prefix")
        if not fallback_env and not fallback_prefix:
            raise ValueError("config/envs.yaml default must set fallback_env or fallback_prefix")
        if fallback_env and fallback_prefix:
            raise ValueError("default.fallback_env and default.fallback_prefix are mutually exclusive")
        self._fallback_env: str | None = str(fallback_env) if fallback_env else None
        self._fallback_prefix: str | None = str(fallback_prefix) if fallback_prefix else None
        self.packages: dict[str, PackageSpec] = self._load_packages(self._raw.get("packages") or {})

    @staticmethod
    def _load_packages(raw: dict) -> dict[str, PackageSpec]:
        packages: dict[str, PackageSpec] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"packages.{name} must be a mapping")
            install_root = entry.get("install_root")
            if not install_root:
                raise ValueError(f"packages.{name} requires install_root")
            packages[str(name)] = PackageSpec(
                name=str(name),
                install_root=str(install_root),
                canary_paths=list(entry.get("canary_paths") or []),
                entrypoints=list(entry.get("entrypoints") or []),
                conform_on_bootstrap=bool(entry.get("conform_on_bootstrap", False)),
                jar_root_ref=str(entry["jar_root_ref"]) if entry.get("jar_root_ref") else None,
            )
        return packages

    def resolve(self, *, stage: str, method_name: str | None = None) -> EnvSpec:
        """Return the resolved env for a method, falling back to stage then default."""
        if method_name:
            method_entry = _entry(self._raw, ("methods", method_name))
            if method_entry is not None:
                return self._build_spec(method_entry, source="method")
        stage_entry = _entry(self._raw, ("stages", stage))
        if stage_entry is not None:
            return self._build_spec(stage_entry, source="stage")
        return EnvSpec(
            conda_bin=self.conda_bin,
            conda_env=self._fallback_env,
            conda_prefix=self._fallback_prefix,
            source="default",
        )

    def _build_spec(self, entry: dict, *, source: Literal["method", "stage", "default"]) -> EnvSpec:
        conda_env = entry.get("conda_env")
        conda_prefix = entry.get("conda_prefix")
        if not conda_env and not conda_prefix:
            conda_env = self._fallback_env
            conda_prefix = self._fallback_prefix
        required = list(entry.get("required_tools") or [])
        package_tools = [str(tool) for tool in (entry.get("package_tools") or [])]
        return EnvSpec(
            conda_bin=self.conda_bin,
            conda_env=str(conda_env) if conda_env else None,
            conda_prefix=str(conda_prefix) if conda_prefix else None,
            required_tools=[str(tool) for tool in required],
            package_tools=package_tools,
            source=source,
        )

    def packages_for_spec(self, spec: EnvSpec) -> list[PackageSpec]:
        """Return the PackageSpecs referenced by a resolved method spec's package_tools."""
        return [self.packages[name] for name in spec.package_tools if name in self.packages]


def check_conda_available(conda_bin: str = "conda") -> bool:
    """Return True if the configured conda binary is on PATH."""
    return shutil.which(conda_bin) is not None


def preflight(
    spec: EnvSpec,
    *,
    runner: "subprocess._Run" | None = None,  # type: ignore[name-defined]
) -> PreflightResult:
    """Run env existence and required-tool probes for one resolved spec.

    Uses `conda run -n NAME command -v TOOL` so neither the current shell nor
    PATH is trusted. `runner` allows tests to substitute a fake subprocess
    runner; when None, :func:`subprocess.run` is used.
    """
    run = runner if runner is not None else subprocess.run
    if not check_conda_available(spec.conda_bin):
        return PreflightResult(
            status="blocked_env",
            env_target=spec.display_target(),
            detail=f"conda binary not found on PATH: {spec.conda_bin}",
        )

    env_probe = [*spec.conda_run_prefix(), "true"]
    env_result = run(env_probe, capture_output=True, text=True, check=False)
    if env_result.returncode != 0:
        return PreflightResult(
            status="blocked_env",
            env_target=spec.display_target(),
            detail=(env_result.stderr or env_result.stdout or "").strip()
            or f"conda env not usable: {spec.display_target()}",
        )

    missing: list[str] = []
    for tool in spec.required_tools:
        probe = [
            *spec.conda_run_prefix(),
            "sh",
            "-c",
            'command -v "$1"',
            "preflight",
            tool,
        ]
        tool_result = run(probe, capture_output=True, text=True, check=False)
        if tool_result.returncode != 0:
            missing.append(tool)

    if missing:
        return PreflightResult(
            status="missing_tool",
            env_target=spec.display_target(),
            missing_tools=missing,
            detail=f"tools missing in {spec.display_target()}: {', '.join(missing)}",
        )

    return PreflightResult(status="ok", env_target=spec.display_target())
