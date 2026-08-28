"""Stage- and method-specific conda environment resolution for local execution.

The resolver loads one active host mapping: ``GENE_AGENT_ENVS_CONFIG`` when it
is set, otherwise the checkout-local ``config/envs.yaml``.  The override picks
a different path; it does not create a merge/overlay hierarchy.  The resolver
answers "what conda env should method X run in" and runs the small preflight
probes (env exists, required tool inside the env).
External software packages (Juicer, 3D-DNA) live in the same file under a
top-level ``packages:`` section and are validated by filesystem checks via
:func:`package_preflight`. Call sites pass the results into
:class:`app.execution.local_runner.LocalRunner`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "envs.yaml"


def active_config_path() -> Path:
    """Return the single active env-config path.

    Honors ``GENE_AGENT_ENVS_CONFIG`` when set.  Relative overrides resolve
    against the caller's current working directory, matching ordinary CLI path
    semantics and ``server_check.sh``.  The default remains checkout-anchored,
    so invoking the CLI from another directory does not select another file.
    """
    override = os.environ.get("GENE_AGENT_ENVS_CONFIG")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)
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
        target = (
            ["-p", self.conda_prefix] if self.conda_prefix else ["-n", self.conda_env]
        )
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
    models: dict[str, str] = Field(default_factory=dict)
    conform_on_bootstrap: bool = False
    jar_root_ref: str | None = None

    @model_validator(mode="after")
    def _normalize_paths(self) -> "PackageSpec":
        self.canary_paths = [_norm_rel(p) for p in self.canary_paths]
        self.entrypoints = [_norm_rel(p) for p in self.entrypoints]
        return self

    def resolved_canary(self, rel: str) -> Path:
        return Path(self.install_root) / rel

    def model_path(self, name: str) -> Path | None:
        """Return a configured model path, relative to ``install_root`` when needed."""
        value = self.models.get(name)
        if not value:
            return None
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return Path(self.install_root).expanduser() / path


class PackagePreflightResult(BaseModel):
    """Outcome of validating one package's filesystem layout."""

    status: Literal["ok", "missing_tool"]
    package: str
    install_root: str
    missing_paths: list[str] = Field(default_factory=list)
    detail: str = ""


def _non_empty_database_text(value: object) -> str:
    """Validate a database registry text field without silently coercing nulls."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("database registry values must be non-empty strings")
    return value.strip()


class BuscoLineageSpec(BaseModel):
    """One immutable BUSCO lineage entry from the host database registry."""

    model_config = ConfigDict(extra="forbid")

    path: str
    version: str
    source: str

    _non_empty_fields = field_validator("path", "version", "source", mode="before")(
        _non_empty_database_text
    )


class BuscoDatabaseSpec(BaseModel):
    """BUSCO database registry and its default lineage."""

    model_config = ConfigDict(extra="forbid")

    default_lineage: str
    lineages: dict[str, BuscoLineageSpec]

    _non_empty_default = field_validator("default_lineage", mode="before")(
        _non_empty_database_text
    )

    @field_validator("lineages", mode="before")
    @classmethod
    def _validate_lineages(cls, value: object) -> object:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("databases.busco.lineages must be a non-empty mapping")
        for name in value:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    "databases.busco.lineages names must be non-empty strings"
                )
        return value

    @model_validator(mode="after")
    def _default_must_exist(self) -> "BuscoDatabaseSpec":
        if self.default_lineage not in self.lineages:
            raise ValueError(
                "databases.busco.default_lineage must name an entry in "
                "databases.busco.lineages"
            )
        return self


class NcbiDatabaseSpec(BaseModel):
    """Root containing NCBI taxonomy snapshots."""

    model_config = ConfigDict(extra="forbid")

    reference_dir: str

    _non_empty_reference_dir = field_validator("reference_dir", mode="before")(
        _non_empty_database_text
    )


class DatabaseRegistry(BaseModel):
    """Typed host database registry frozen with a run's dependencies."""

    model_config = ConfigDict(extra="forbid")

    busco: BuscoDatabaseSpec
    ncbi: NcbiDatabaseSpec

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_ncbi_metazoa(cls, value: object) -> object:
        """Accept the former single-reference shape without emitting it."""
        if not isinstance(value, Mapping):
            return value
        migrated = dict(value)
        legacy = migrated.pop("ncbi_metazoa", None)
        if legacy is not None:
            if "ncbi" in migrated:
                raise ValueError(
                    "databases cannot declare both ncbi and legacy ncbi_metazoa"
                )
            migrated["ncbi"] = legacy
        return migrated

    @property
    def ncbi_metazoa(self) -> NcbiDatabaseSpec:
        """Read-only compatibility view for callers loading an old host file."""
        return self.ncbi


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
            detail=(
                "install_root does not exist or is not a directory: "
                f"{package.install_root}"
            ),
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


ConfigMapping = dict[str, Any]


def _resolve_database_path(value: str, config_root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_root / path
    return str(path.resolve(strict=False))


def _load_raw(config_path: Path) -> ConfigMapping:
    if not config_path.exists():
        raise FileNotFoundError(f"env config missing: {config_path}")
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"env config YAML is invalid: {config_path}: {exc}") from exc
    return _validated_raw(raw, source=config_path)


def _load_raw_with_identity(config_path: Path) -> tuple[ConfigMapping, str]:
    """Parse one byte snapshot and return its content identity."""
    if not config_path.exists():
        raise FileNotFoundError(f"env config missing: {config_path}")
    try:
        raw_bytes = config_path.read_bytes()
        raw = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except UnicodeDecodeError as exc:
        raise ValueError(f"env config is not valid UTF-8: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"env config YAML is invalid: {config_path}: {exc}") from exc
    return (
        _validated_raw(raw, source=config_path),
        sha256(raw_bytes).hexdigest(),
    )


def _validated_raw(raw: object, *, source: Path) -> ConfigMapping:
    if not isinstance(raw, Mapping):
        raise ValueError(f"env config must be a mapping: {source}")
    return cast(ConfigMapping, deepcopy(dict(raw)))


def _entry(raw: ConfigMapping, keys: tuple[str, ...]) -> ConfigMapping | None:
    node: object = raw
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return cast(ConfigMapping, node) if isinstance(node, dict) else None


class EnvResolver:
    """Resolve conda environments for stages and methods from the active config."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        config_data: Mapping[str, Any] | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path) if config_path is not None else active_config_path()
        )
        self.config_sha256: str | None
        if config_data is None:
            self._raw, self.config_sha256 = _load_raw_with_identity(self.config_path)
        else:
            self._raw = _validated_raw(config_data, source=self.config_path)
            self.config_sha256 = None
        default_section = self._raw.get("default") or {}
        if not isinstance(default_section, Mapping):
            raise ValueError("default config section must be a mapping")
        self.conda_bin: str = str(default_section.get("conda_bin") or "conda")
        fallback_env = default_section.get("fallback_env")
        fallback_prefix = default_section.get("fallback_prefix")
        if not fallback_env and not fallback_prefix:
            raise ValueError(
                "config/envs.yaml default must set fallback_env or fallback_prefix"
            )
        if fallback_env and fallback_prefix:
            raise ValueError(
                "default.fallback_env and default.fallback_prefix are mutually "
                "exclusive"
            )
        self._fallback_env: str | None = str(fallback_env) if fallback_env else None
        self._fallback_prefix: str | None = (
            str(fallback_prefix) if fallback_prefix else None
        )
        self.databases: DatabaseRegistry | None = self._load_database_registry()
        packages = self._raw.get("packages") or {}
        if not isinstance(packages, Mapping):
            raise ValueError("packages config section must be a mapping")
        self.packages = self._load_packages(dict(packages))
        self._validate_environment_entries()

    def _load_database_registry(self) -> DatabaseRegistry | None:
        """Load the optional host registry, rejecting malformed declarations.

        Older host files may omit ``databases`` entirely.  Once the section is
        present it is a strict contract: both supported database families are
        required and Pydantic forbids unknown or empty fields.
        """
        if "databases" not in self._raw:
            return None
        raw_databases = self._raw["databases"]
        if not isinstance(raw_databases, Mapping):
            raise ValueError("databases config section must be a mapping")
        try:
            registry = DatabaseRegistry.model_validate(raw_databases)
        except ValidationError as exc:
            raise ValueError(f"databases config section is invalid: {exc}") from exc
        # Database paths are host-local config values.  Resolve relative and
        # ``~`` values once, against the directory containing this envs.yaml,
        # so active and frozen resolvers share exactly the same identity.
        config_root = self.config_path.expanduser().resolve(strict=False).parent
        busco = registry.busco.model_copy(
            update={
                "lineages": {
                    name: spec.model_copy(
                        update={"path": _resolve_database_path(spec.path, config_root)}
                    )
                    for name, spec in registry.busco.lineages.items()
                }
            }
        )
        ncbi = registry.ncbi.model_copy(
            update={
                "reference_dir": _resolve_database_path(
                    registry.ncbi.reference_dir, config_root
                )
            }
        )
        return registry.model_copy(update={"busco": busco, "ncbi": ncbi})

    @staticmethod
    def _load_packages(raw: ConfigMapping) -> dict[str, PackageSpec]:
        packages: dict[str, PackageSpec] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"packages.{name} must be a mapping")
            install_root = entry.get("install_root")
            if not install_root:
                raise ValueError(f"packages.{name} requires install_root")
            models = entry.get("models") or {}
            if not isinstance(models, dict):
                raise ValueError(f"packages.{name}.models must be a mapping")
            packages[str(name)] = PackageSpec(
                name=str(name),
                install_root=str(install_root),
                canary_paths=list(entry.get("canary_paths") or []),
                entrypoints=list(entry.get("entrypoints") or []),
                models={
                    str(model_name): str(path) for model_name, path in models.items()
                },
                conform_on_bootstrap=bool(entry.get("conform_on_bootstrap", False)),
                jar_root_ref=str(entry["jar_root_ref"])
                if entry.get("jar_root_ref")
                else None,
            )
        return packages

    def _validate_environment_entries(self) -> None:
        """Eagerly validate all declared stage/method overrides and packages."""

        sections: tuple[tuple[str, Literal["stage", "method"]], ...] = (
            ("stages", "stage"),
            ("methods", "method"),
        )
        for section_name, source in sections:
            raw_section = self._raw.get(section_name)
            if raw_section is None:
                continue
            if not isinstance(raw_section, Mapping):
                raise ValueError(f"{section_name} config section must be a mapping")
            for name, entry in raw_section.items():
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{section_name}.{name} must be a mapping")
                # Package references are checked by ``packages_for_spec`` at
                # the execution/bootstrap consumer boundary. Keeping this
                # loader check focused on the shape of environment entries
                # lets callers report which method references the missing
                # package instead of failing during resolver construction.
                self._build_spec(dict(entry), source=source)

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

    def resolve_busco_lineage(self, name: str | None = None) -> BuscoLineageSpec:
        """Resolve a BUSCO lineage from the typed host registry.

        A missing registry is intentionally an error instead of a fallback to
        run-config values: database paths belong to the host contract and are
        frozen before execution.
        """
        if self.databases is None:
            raise ValueError("databases.busco registry is not configured")
        lineage_name = (
            name if name is not None else self.databases.busco.default_lineage
        )
        try:
            return self.databases.busco.lineages[lineage_name]
        except KeyError as exc:
            raise ValueError(
                f"databases.busco.lineages has no configured lineage: {lineage_name}"
            ) from exc

    def configured_method_specs(self) -> list[tuple[str, EnvSpec]]:
        """Return every configured method and its effective environment spec.

        Bootstrap uses this view to validate the same method-level precedence
        that stage execution uses. Method names are configuration keys and the
        first dotted component is their owning stage.
        """
        raw_methods = self._raw.get("methods") or {}
        if not isinstance(raw_methods, Mapping):
            raise ValueError("methods config section must be a mapping")
        entries: list[tuple[str, EnvSpec]] = []
        for raw_name in raw_methods:
            method_name = str(raw_name).strip()
            if not method_name:
                raise ValueError("methods config contains an empty method name")
            stage = method_name.split(".", 1)[0]
            entries.append(
                (
                    method_name,
                    self.resolve(stage=stage, method_name=method_name),
                )
            )
        return entries

    def _build_spec(
        self,
        entry: ConfigMapping,
        *,
        source: Literal["method", "stage", "default"],
    ) -> EnvSpec:
        conda_env = entry.get("conda_env")
        conda_prefix = entry.get("conda_prefix")
        if not conda_env and not conda_prefix:
            conda_env = self._fallback_env
            conda_prefix = self._fallback_prefix
        required = entry.get("required_tools") or []
        package_tools = entry.get("package_tools") or []
        if isinstance(required, (str, bytes)) or not isinstance(required, Sequence):
            raise ValueError("required_tools must be a list")
        if isinstance(package_tools, (str, bytes)) or not isinstance(
            package_tools, Sequence
        ):
            raise ValueError("package_tools must be a list")
        return EnvSpec(
            conda_bin=self.conda_bin,
            conda_env=str(conda_env) if conda_env else None,
            conda_prefix=str(conda_prefix) if conda_prefix else None,
            required_tools=[str(tool) for tool in required],
            package_tools=[str(tool) for tool in package_tools],
            source=source,
        )

    def packages_for_spec(self, spec: EnvSpec) -> list[PackageSpec]:
        "Return the PackageSpecs referenced by a resolved method spec's package_tools."
        missing = [name for name in spec.package_tools if name not in self.packages]
        if missing:
            raise ValueError(
                "env spec references undefined package_tools: " + ", ".join(missing)
            )
        return [self.packages[name] for name in spec.package_tools]


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
