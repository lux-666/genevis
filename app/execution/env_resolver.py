"""Stage- and method-specific conda environment resolution for local execution.

The resolver loads `config/envs.yaml`, answers "what conda env should method X
run in", and runs the small preflight probes (env exists, required tool inside
the env). Call sites pass the results into :class:`app.execution.local_runner.LocalRunner`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


DEFAULT_CONFIG_PATH = Path("config/envs.yaml")

PreflightStatus = Literal["ok", "blocked_env", "missing_tool"]


class EnvSpec(BaseModel):
    """Resolved conda environment for one method or stage."""

    conda_bin: str = "conda"
    conda_env: str | None = None
    conda_prefix: str | None = None
    required_tools: list[str] = Field(default_factory=list)
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
    """Resolve conda environments for stages and methods from envs.yaml."""

    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        self.config_path = Path(config_path)
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
        return EnvSpec(
            conda_bin=self.conda_bin,
            conda_env=str(conda_env) if conda_env else None,
            conda_prefix=str(conda_prefix) if conda_prefix else None,
            required_tools=[str(tool) for tool in required],
            source=source,
        )


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
