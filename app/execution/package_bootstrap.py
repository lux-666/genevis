"""Bootstrap utilities for external software packages.

Provides validation and one-time deployment operations for external packages
declared in config/envs.yaml. Bootstrap actions are idempotent and safe:
symlinks are only created if missing, and all filesystem writes are confined
to the user-declared install_root directories.

Primary use case: conforming Juicer's raw git-clone layout to the structure
its entry scripts expect (scripts/ -> CPU/, and juicer_tools.jar symlinked
into scripts/common/).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.execution.env_resolver import EnvResolver, PackagePreflightResult, PackageSpec, package_preflight


@dataclass
class ConformanceResult:
    """Result of a package conformance operation."""

    package_name: str
    status: str  # "ok", "skipped", "error"
    actions_taken: list[str]
    detail: str


def conform_juicer(
    juicer_spec: PackageSpec,
    juicer_tools_spec: PackageSpec | None,
) -> ConformanceResult:
    """Conform a Juicer installation to the expected layout.

    Creates:
    - scripts/ -> CPU/ symlink (if missing)
    - scripts/common/juicer_tools.jar -> <juicer_tools_root>/juicer_tools.jar
      (if jar_root_ref is provided and jar is missing)

    Returns a ConformanceResult describing what was done. Idempotent: if the
    target symlinks already exist and point to the expected locations, this
    is a no-op.
    """
    juicer_root = Path(juicer_spec.install_root)
    actions = []

    # Step 1: scripts -> CPU symlink
    scripts_dir = juicer_root / "scripts"
    cpu_dir = juicer_root / "CPU"

    if not cpu_dir.exists():
        return ConformanceResult(
            package_name="juicer",
            status="error",
            actions_taken=actions,
            detail=f"CPU/ directory not found at {cpu_dir}",
        )

    if scripts_dir.exists():
        if scripts_dir.is_symlink():
            target = scripts_dir.resolve()
            if target == cpu_dir.resolve():
                actions.append(f"scripts -> CPU symlink already exists")
            else:
                return ConformanceResult(
                    package_name="juicer",
                    status="error",
                    actions_taken=actions,
                    detail=f"scripts/ exists but points to {target}, expected {cpu_dir}",
                )
        else:
            return ConformanceResult(
                package_name="juicer",
                status="error",
                actions_taken=actions,
                detail=f"scripts/ exists as a real directory, not a symlink. Please remove or rename it.",
            )
    else:
        scripts_dir.symlink_to("CPU", target_is_directory=True)
        actions.append(f"Created symlink: scripts -> CPU")

    # Step 2: jar symlink (if jar_root_ref is provided)
    if juicer_tools_spec:
        jar_target_dir = juicer_root / "scripts" / "common"
        jar_target = jar_target_dir / "juicer_tools.jar"
        jar_source = Path(juicer_tools_spec.install_root) / "juicer_tools.jar"

        if not jar_source.exists():
            return ConformanceResult(
                package_name="juicer",
                status="error",
                actions_taken=actions,
                detail=f"juicer_tools.jar not found at {jar_source}",
            )

        if not jar_target_dir.exists():
            return ConformanceResult(
                package_name="juicer",
                status="error",
                actions_taken=actions,
                detail=f"scripts/common/ directory not found at {jar_target_dir}",
            )

        if jar_target.exists():
            if jar_target.is_symlink():
                target = jar_target.resolve()
                if target == jar_source.resolve():
                    actions.append(f"juicer_tools.jar symlink already exists")
                else:
                    return ConformanceResult(
                        package_name="juicer",
                        status="error",
                        actions_taken=actions,
                        detail=f"juicer_tools.jar exists but points to {target}, expected {jar_source}",
                    )
            else:
                actions.append(f"juicer_tools.jar already exists (real file)")
        else:
            jar_target.symlink_to(jar_source)
            actions.append(f"Created symlink: scripts/common/juicer_tools.jar -> {jar_source}")

    return ConformanceResult(
        package_name="juicer",
        status="ok",
        actions_taken=actions,
        detail="Juicer conformance complete",
    )


def preflight_all(resolver: EnvResolver) -> list[tuple[str, PackagePreflightResult]]:
    """Run preflight checks on all declared packages.

    Returns a list of (package_name, result) tuples.
    """
    results = []
    for package_name, package_spec in resolver.packages.items():
        result = package_preflight(package_spec)
        results.append((package_name, result))
    return results


def verify_envs_yaml(config_path: Path) -> list[str]:
    """Verify that envs.yaml exists and is properly configured.

    Returns a list of issues (empty if valid). Checks:
    - File exists
    - default.fallback_env or default.fallback_prefix is set and not a placeholder
    - package install_root paths exist and are not placeholders
    """
    issues = []

    if not config_path.exists():
        issues.append(f"Configuration file not found: {config_path}")
        return issues

    try:
        resolver = EnvResolver(config_path)
    except Exception as e:
        issues.append(f"Failed to load configuration: {e}")
        return issues

    # Check default fallback is set
    default_config: dict[str, Any] = resolver._raw.get("default", {})
    fallback_env = default_config.get("fallback_env")
    fallback_prefix = default_config.get("fallback_prefix")

    if not fallback_env and not fallback_prefix:
        issues.append("default.fallback_env or default.fallback_prefix must be set")
    elif fallback_env and "your_tool_env" in str(fallback_env):
        issues.append(f"default.fallback_env is still a placeholder: {fallback_env}")
    elif fallback_prefix and "/path/to/" in str(fallback_prefix):
        issues.append(f"default.fallback_prefix is still a placeholder: {fallback_prefix}")

    # Check package install_root paths
    for package_name, package_spec in resolver.packages.items():
        install_root = package_spec.install_root
        if "/path/to/" in install_root:
            issues.append(f"packages.{package_name}.install_root is still a placeholder: {install_root}")
        elif not Path(install_root).exists():
            issues.append(f"packages.{package_name}.install_root does not exist: {install_root}")

    autohic = resolver.packages.get("autohic")
    if autohic is not None:
        for model_name in ("error", "chromosome"):
            configured = autohic.models.get(model_name)
            field = f"packages.autohic.models.{model_name}"
            if not configured:
                continue
            model_path = autohic.model_path(model_name)
            if "/path/to/" in configured:
                issues.append(f"{field} is still a placeholder: {configured}")
            elif model_path is None or not model_path.is_file():
                issues.append(f"{field} does not exist: {model_path}")

    return issues
