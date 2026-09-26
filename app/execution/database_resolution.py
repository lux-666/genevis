"""Resolve and validate host-owned databases at the freeze/execution boundary.

``RunConfig`` may retain a legacy logical BUSCO lineage assertion, but the
manifest scientific name selects the lineage for new runs. This module is the
narrow adapter shared by readiness, freeze, and stage consumers. It accepts
both the active and frozen dependency resolver interfaces and returns only the
compact execution projection needed by BUSCO/NCBI.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from app.schemas.run_config import (
    BuscoRunParameters,
    RunConfig,
    validate_busco_lineage,
)


class DatabaseResolutionError(ValueError):
    """Raised when a requested database cannot be verified fail-closed."""


class DatabaseResolver(Protocol):
    databases: Any
    config_path: Path

    def resolve_busco_lineage(self, name: str | None = None) -> Any: ...


def _require_registry(resolver: DatabaseResolver) -> Any:
    registry = getattr(resolver, "databases", None)
    if registry is None:
        raise DatabaseResolutionError(
            "host database registry is missing; configure databases.busco and "
            "databases.ncbi.reference_dir in envs.yaml"
        )
    return registry


def canonical_database_path(value: str, resolver: DatabaseResolver) -> Path:
    """Resolve a host database path with envs.yaml-relative semantics."""

    path = Path(value).expanduser()
    config_path = Path(getattr(resolver, "config_path", "envs.yaml"))
    if not path.is_absolute():
        path = config_path.expanduser().resolve(strict=False).parent / path
    return path.resolve(strict=False)


def _busco_request(run_config: RunConfig) -> BuscoRunParameters:
    review = run_config.assembly_review
    if review is None or review.busco is None:
        return BuscoRunParameters()
    return review.busco


def resolve_busco_parameters(
    run_config: RunConfig,
    resolver: DatabaseResolver,
    *,
    species_name: str | None = None,
    related_species_reference_dir: str | None = None,
) -> dict[str, Any]:
    """Return a complete, validated BUSCO execution projection.

    The manifest species selects one compatible host lineage. A legacy RunConfig
    ``lineage`` may assert that identity but cannot override it. Filesystem paths,
    version, and provenance come exclusively from the host registry.
    """

    registry = _require_registry(resolver)
    request = _busco_request(run_config)
    busco_registry = getattr(registry, "busco", None)
    if busco_registry is None:
        raise DatabaseResolutionError("databases.busco is not configured")
    logical_name = request.lineage
    if species_name is not None:
        from app.reference.ncbi import resolve_busco_lineage_for_species

        reference_dir = related_species_reference_dir or resolve_ncbi_reference_dir(
            resolver, species_name=species_name
        )
        try:
            derived_lineage = resolve_busco_lineage_for_species(
                species_name,
                reference_dir=reference_dir,
                configured_lineages=set(busco_registry.lineages),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise DatabaseResolutionError(
                "input_manifest.user_metadata.target_species cannot resolve a "
                f"unique installed BUSCO lineage: {exc}"
            ) from exc
        if logical_name is not None and logical_name != derived_lineage:
            raise DatabaseResolutionError(
                "assembly_review.busco.lineage conflicts with the species-derived "
                f"lineage: {logical_name!r} versus {derived_lineage!r}"
            )
        logical_name = derived_lineage
    if logical_name is None:
        logical_name = str(getattr(busco_registry, "default_lineage", "")).strip()
    if not logical_name:
        raise DatabaseResolutionError("BUSCO logical lineage is empty")

    try:
        spec = resolver.resolve_busco_lineage(logical_name)
    except (KeyError, TypeError, ValueError) as exc:
        raise DatabaseResolutionError(
            f"BUSCO lineage is not configured: {logical_name}"
        ) from exc
    path = str(getattr(spec, "path", "")).strip()
    version = str(getattr(spec, "version", "")).strip()
    source = str(getattr(spec, "source", "")).strip()
    if not path or not version or not source:
        raise DatabaseResolutionError(
            f"BUSCO lineage registry entry is incomplete: {logical_name}"
        )
    lineage_path = canonical_database_path(path, resolver)
    if not lineage_path.is_dir():
        raise DatabaseResolutionError(
            f"BUSCO lineage path does not exist or is not a directory: {lineage_path}"
        )
    try:
        validate_busco_lineage(
            lineage_path,
            lineage_name=logical_name,
            lineage_version=version,
            lineage_source=source,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseResolutionError(
            f"BUSCO lineage failed structural validation: {logical_name}: {exc}"
        ) from exc
    return {
        "lineage_path": str(lineage_path.resolve(strict=False)),
        "lineage_name": logical_name,
        "lineage_version": version,
        "lineage_source": source,
        "mode": request.mode,
        "threads": request.threads,
    }


def resolve_ncbi_reference(
    resolver: DatabaseResolver,
    *,
    species_name: str | None,
) -> tuple[str, str]:
    """Select the snapshot with the best available related-species evidence."""

    registry = _require_registry(resolver)
    ncbi = getattr(registry, "ncbi", None)
    reference_root = str(getattr(ncbi, "reference_dir", "")).strip()
    if not reference_root:
        raise DatabaseResolutionError("databases.ncbi.reference_dir is empty")

    from app.reference.ncbi import (
        MATCHING_TIER_RANK,
        build_related_species_evidence_pack,
        discover_reference_snapshots,
        reference_snapshot_sha256,
        validate_reference_snapshot,
    )

    root = canonical_database_path(reference_root, resolver)
    try:
        references = discover_reference_snapshots(root)
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseResolutionError(
            f"NCBI reference root failed discovery: {exc}"
        ) from exc

    matches: list[tuple[int, int, str, Path]] = []
    failures: list[str] = []
    for name, path in sorted(references.items()):
        try:
            validate_reference_snapshot(path)
            expected_hashes = getattr(resolver, "ncbi_manifest_sha256s", {})
            expected_hash = (
                expected_hashes.get(name)
                if isinstance(expected_hashes, Mapping)
                else None
            )
            if expected_hash is None and name == "metazoa":
                expected_hash = getattr(resolver, "ncbi_manifest_sha256", None)
            if (
                expected_hash is not None
                and reference_snapshot_sha256(path) != expected_hash
            ):
                raise ValueError(
                    "NCBI reference snapshot changed after dependency freeze"
                )
        except (OSError, TypeError, ValueError) as exc:
            raise DatabaseResolutionError(
                f"NCBI reference {name!r} failed integrity validation: {exc}"
            ) from exc

        if species_name is None:
            matches.append((0, 0, str(name), path))
            continue
        pack = build_related_species_evidence_pack(
            str(species_name), reference_dir=path
        )
        if pack.get("status") == "available":
            mode = str(pack.get("matching_mode") or "")
            records = pack.get("selected_records")
            matches.append(
                (
                    MATCHING_TIER_RANK.get(mode, len(MATCHING_TIER_RANK)),
                    -len(records) if isinstance(records, list) else 0,
                    str(name),
                    path,
                )
            )
        else:
            failures.append(
                f"{name}={','.join(str(note) for note in pack.get('notes') or [])}"
            )

    if not matches:
        raise DatabaseResolutionError(
            "input_manifest.user_metadata.target_species "
            f"{species_name!r} found no usable exact, same-genus, same-family, "
            "or same-order evidence in the configured NCBI references; "
            f"reference results={'; '.join(failures)}"
        )
    _rank, _record_count, name, path = min(matches)
    return name, str(path.resolve(strict=False))


def resolve_ncbi_reference_dir(
    resolver: DatabaseResolver,
    *,
    species_name: str | None = None,
) -> str:
    """Return the selected, verified NCBI reference directory."""

    _name, path = resolve_ncbi_reference(resolver, species_name=species_name)
    return path


def validate_frozen_busco_parameters(
    parameters: Mapping[str, Any],
    resolver: DatabaseResolver,
) -> dict[str, Any]:
    """Validate the compact BUSCO projection against the frozen registry."""

    required = ("lineage_path", "lineage_name", "lineage_version", "lineage_source")
    if any(not str(parameters.get(key) or "").strip() for key in required):
        raise DatabaseResolutionError("frozen BUSCO parameters are incomplete")
    try:
        spec = resolver.resolve_busco_lineage(str(parameters["lineage_name"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DatabaseResolutionError(
            f"frozen BUSCO lineage is absent from the host registry: "
            f"{parameters.get('lineage_name')}"
        ) from exc
    expected = {
        "lineage_path": str(
            canonical_database_path(str(getattr(spec, "path", "")), resolver)
        ),
        "lineage_name": str(parameters["lineage_name"]),
        "lineage_version": str(getattr(spec, "version", "")),
        "lineage_source": str(getattr(spec, "source", "")),
    }
    if any(str(parameters.get(key)) != value for key, value in expected.items()):
        raise DatabaseResolutionError("frozen BUSCO parameters disagree with registry")
    mode = parameters.get("mode")
    if mode != "genome":
        raise DatabaseResolutionError("frozen BUSCO mode must be 'genome'")
    threads = parameters.get("threads")
    if type(threads) is not int or threads <= 0:
        raise DatabaseResolutionError("frozen BUSCO threads must be positive")
    path = canonical_database_path(expected["lineage_path"], resolver)
    if not path.is_dir():
        raise DatabaseResolutionError(
            f"frozen BUSCO lineage path is unavailable: {path}"
        )
    try:
        validate_busco_lineage(
            path,
            lineage_name=expected["lineage_name"],
            lineage_version=expected["lineage_version"],
            lineage_source=str(parameters["lineage_source"]),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseResolutionError(
            f"frozen BUSCO lineage validation failed: {exc}"
        ) from exc
    return {
        **expected,
        "mode": mode,
        "threads": threads,
    }


def validate_review_databases(
    run_config: RunConfig,
    resolver: DatabaseResolver,
    *,
    species_name: str | None = None,
) -> dict[str, Any]:
    """Validate all Assembly Review databases and return compact projections."""

    if "assembly_review" not in run_config.requested_stage_names():
        return {}
    _ncbi_name, ncbi = resolve_ncbi_reference(resolver, species_name=species_name)
    busco = resolve_busco_parameters(
        run_config,
        resolver,
        species_name=species_name,
        related_species_reference_dir=ncbi,
    )
    return {
        "busco_parameters": busco,
        "related_species_reference_dir": ncbi,
    }
