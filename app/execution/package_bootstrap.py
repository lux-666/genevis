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

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.execution.env_resolver import (
    EnvResolver,
    PackagePreflightResult,
    PackageSpec,
    package_preflight,
)


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
    *,
    apply: bool = True,
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
    actions: list[str] = []

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

    if scripts_dir.exists() or scripts_dir.is_symlink():
        if scripts_dir.is_symlink():
            target = scripts_dir.resolve()
            if target == cpu_dir.resolve():
                actions.append("scripts -> CPU symlink already exists")
            else:
                return ConformanceResult(
                    package_name="juicer",
                    status="error",
                    actions_taken=actions,
                    detail=(
                        f"scripts/ exists but points to {target}, expected {cpu_dir}"
                    ),
                )
        else:
            return ConformanceResult(
                package_name="juicer",
                status="error",
                actions_taken=actions,
                detail=(
                    "scripts/ exists as a real directory, not a symlink. Please "
                    "remove or rename it."
                ),
            )
    else:
        if apply:
            scripts_dir.symlink_to("CPU", target_is_directory=True)
            actions.append("Created symlink: scripts -> CPU")
        else:
            actions.append("Would create symlink: scripts -> CPU")

    # Step 2: jar symlink (if jar_root_ref is provided)
    if juicer_spec.jar_root_ref and juicer_tools_spec is None:
        return ConformanceResult(
            package_name="juicer",
            status="error",
            actions_taken=actions,
            detail=(
                f"referenced jar package is not configured: {juicer_spec.jar_root_ref}"
            ),
        )
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

        effective_target_dir = (
            jar_target_dir if scripts_dir.exists() else cpu_dir / "common"
        )
        effective_target = effective_target_dir / "juicer_tools.jar"
        if not effective_target_dir.exists():
            return ConformanceResult(
                package_name="juicer",
                status="error",
                actions_taken=actions,
                detail=(
                    "scripts/common/ source directory not found at "
                    f"{effective_target_dir}"
                ),
            )

        if effective_target.exists():
            if effective_target.is_symlink():
                target = effective_target.resolve()
                if target == jar_source.resolve():
                    actions.append("juicer_tools.jar symlink already exists")
                else:
                    return ConformanceResult(
                        package_name="juicer",
                        status="error",
                        actions_taken=actions,
                        detail=(
                            f"juicer_tools.jar exists but points to {target}, "
                            f"expected {jar_source}"
                        ),
                    )
            else:
                actions.append("juicer_tools.jar already exists (real file)")
        else:
            if apply:
                jar_target.symlink_to(jar_source)
                actions.append(
                    f"Created symlink: scripts/common/juicer_tools.jar -> {jar_source}"
                )
            else:
                actions.append(
                    "Would create symlink: scripts/common/juicer_tools.jar -> "
                    f"{jar_source}"
                )

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


def verify_envs_yaml(
    config_path: Path,
    *,
    config_data: Mapping[str, Any] | None = None,
    conformance_packages: Collection[str] = (),
) -> list[str]:
    """Verify that envs.yaml exists and is properly configured.

    Returns a list of issues (empty if valid). Checks:
    - File exists
    - default.fallback_env or default.fallback_prefix is set and not a placeholder
    - package install_root paths exist and are not placeholders
    """
    issues = []

    if config_data is None and not config_path.exists():
        issues.append(f"Configuration file not found: {config_path}")
        return issues

    try:
        resolver = EnvResolver(config_path, config_data=config_data)
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
        issues.append(
            f"default.fallback_prefix is still a placeholder: {fallback_prefix}"
        )

    # Keep package-reference errors attached to the declaring stage/method so
    # bootstrap can report the exact config entry instead of failing while the
    # resolver is being constructed.
    for section_name, names in (
        ("stages", (resolver._raw.get("stages") or {}).keys()),
        ("methods", (resolver._raw.get("methods") or {}).keys()),
    ):
        for raw_name in names:
            name = str(raw_name)
            stage = name.split(".", 1)[0]
            spec = resolver.resolve(
                stage=stage,
                method_name=name if section_name == "methods" else None,
            )
            try:
                resolver.packages_for_spec(spec)
            except ValueError as exc:
                issues.append(f"{section_name}.{name}: {exc}")

    # Check package install_root paths, models, and declared canaries.
    for package_name, package_spec in resolver.packages.items():
        install_root = package_spec.install_root
        if "/path/to/" in install_root:
            issues.append(
                f"packages.{package_name}.install_root is still a placeholder: "
                f"{install_root}"
            )
        elif not Path(install_root).exists():
            issues.append(
                f"packages.{package_name}.install_root does not exist: {install_root}"
            )

    allowed_missing: dict[str, set[str]] = {}
    if "juicer" in conformance_packages:
        juicer = resolver.packages.get("juicer")
        if juicer is not None and juicer.conform_on_bootstrap:
            jar_spec = (
                resolver.packages.get(juicer.jar_root_ref)
                if juicer.jar_root_ref
                else None
            )
            conformance_result = conform_juicer(juicer, jar_spec, apply=False)
            if conformance_result.status != "ok":
                issues.append(
                    f"packages.juicer conformance: {conformance_result.detail}"
                )
            elif jar_spec is not None:
                allowed_missing["juicer"] = {"scripts/common/juicer_tools.jar"}

    for package_name, package_spec in resolver.packages.items():
        preflight_result = package_preflight(package_spec)
        missing = [
            path
            for path in preflight_result.missing_paths
            if path not in allowed_missing.get(package_name, set())
        ]
        if preflight_result.status != "ok" and missing:
            issues.append(
                f"packages.{package_name} failed preflight: " + ", ".join(missing)
            )

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

    # Database declarations are part of the same host configuration lifecycle.
    # Bootstrap validates every configured entry, while run readiness later
    # narrows that catalog to the databases required by the selected workflow.
    if resolver.databases is None:
        issues.append(
            "databases registry is missing; configure databases.busco and "
            "databases.ncbi_metazoa in the active envs.yaml"
        )
    else:
        from app.execution.database_resolution import canonical_database_path
        from app.reference.ncbi_metazoa import validate_reference_snapshot
        from app.schemas.run_config import validate_busco_lineage

        for lineage_name, lineage in resolver.databases.busco.lineages.items():
            field = f"databases.busco.lineages.{lineage_name}.path"
            lineage_path = canonical_database_path(lineage.path, resolver)
            if "/path/to/" in lineage.path:
                issues.append(f"{field} is still a placeholder: {lineage.path}")
                continue
            if not lineage_path.is_dir():
                issues.append(f"{field} does not exist: {lineage_path}")
                continue
            try:
                validate_busco_lineage(
                    lineage_path,
                    lineage_name=lineage_name,
                    lineage_version=lineage.version,
                    lineage_source=lineage.source,
                )
            except (OSError, TypeError, ValueError) as exc:
                issues.append(f"{field} failed validation: {exc}")

        ncbi_path = resolver.databases.ncbi_metazoa.reference_dir
        ncbi_field = "databases.ncbi_metazoa.reference_dir"
        if "/path/to/" in ncbi_path:
            issues.append(f"{ncbi_field} is still a placeholder: {ncbi_path}")
        else:
            try:
                validate_reference_snapshot(
                    canonical_database_path(ncbi_path, resolver)
                )
            except (OSError, TypeError, ValueError) as exc:
                issues.append(f"{ncbi_field} failed validation: {exc}")

    return issues
