from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.execution.database_resolution import (
    DatabaseResolutionError,
    resolve_busco_parameters,
    resolve_ncbi_reference,
    resolve_ncbi_reference_dir,
)
from app.execution.dependency_snapshot import (
    FrozenDependencyResolver,
    capture_dependency_snapshot,
)
from app.execution.env_resolver import EnvResolver
from app.reference.ncbi import MATCHING_TIER_RANK, MATCHING_TIERS
from app.schemas.run_config import RunConfig
from tests.busco_fixture import ensure_busco_lineage


def _write_reference(reference: Path, files: dict[str, str]) -> None:
    tsv_name = next(name for name in files if name.endswith(".ncbi.tsv"))
    prefix = tsv_name.removesuffix(".ncbi.tsv")
    lineage = files[f"{prefix}.ncbi.lineage"]
    native = {
        f"{prefix}.ncbi.json": "{}\n",
        f"{prefix}.ncbi.lineage": lineage,
        f"{prefix}.ncbi.lineage2": lineage,
        tsv_name: files[tsv_name],
    }
    for name, content in native.items():
        (reference / name).write_text(content, encoding="utf-8")


def _write_reference_files(reference: Path) -> None:
    _write_reference(
        reference,
        {
            "Metazoa.ncbi.tsv": "accession\n",
            "Metazoa.ncbi.lineage": "Organism Taxonomic ID\n",
        },
    )


def _write_species_reference(
    reference: Path,
    *,
    species_name: str,
    lineage_name: str,
    prefix: str = "Metazoa",
) -> None:
    _write_reference(
        reference,
        {
            f"{prefix}.ncbi.tsv": (
                "Current Accession\tOrganism Name\tOrganism Taxonomic ID\t"
                "Annotation BUSCO Lineage\n"
                f"GCF_000000001.1\t{species_name}\t1\t{lineage_name}\n"
            ),
            f"{prefix}.ncbi.lineage": (
                f"Organism Taxonomic ID\tLineage\n1\tMetazoa;{species_name}\n"
            ),
        },
    )


def _config(lineage: Path, ncbi: Path) -> dict[str, object]:
    return {
        "default": {"conda_bin": "/usr/bin/true", "fallback_env": "tools"},
        "databases": {
            "busco": {
                "default_lineage": lineage.name,
                "lineages": {
                    lineage.name: {
                        "path": str(lineage),
                        "version": "odb10",
                        "source": "test",
                    }
                },
            },
            "ncbi_metazoa": {"reference_dir": str(ncbi)},
        },
        "methods": {},
        "stages": {},
        "packages": {},
    }


def _run_config(**busco: object) -> RunConfig:
    return RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly_review", "allow_local_execution": True}
            ],
            "assembly_review": {"busco": busco},
        }
    )


def _multi_reference_resolver(
    tmp_path: Path, lineage: Path
) -> tuple[EnvResolver, Path, Path]:
    root = tmp_path / "ncbi"
    metazoa = root / "metazoa"
    viridiplantae = root / "viridiplantae"
    metazoa.mkdir(parents=True)
    viridiplantae.mkdir(parents=True)
    _write_species_reference(
        metazoa,
        species_name="Solea ovata",
        lineage_name=lineage.name,
    )
    _write_species_reference(
        viridiplantae,
        species_name="Arabidopsis thaliana",
        lineage_name="embryophyta_odb10",
        prefix="Viridiplantae",
    )
    config = _config(lineage, metazoa)
    config["databases"] = {
        **config["databases"],
        "ncbi": {"reference_dir": str(root)},
    }
    config["databases"].pop("ncbi_metazoa")
    return EnvResolver(config_data=config), metazoa, viridiplantae


def test_matching_tier_order_has_one_canonical_contract() -> None:
    assert MATCHING_TIERS == ("exact", "same_genus", "same_family", "same_order")
    assert MATCHING_TIER_RANK == {
        tier: rank for rank, tier in enumerate(MATCHING_TIERS)
    }


def test_logical_lineage_resolves_to_complete_projection(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_files(ncbi)
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(
        _run_config(lineage=lineage.name, threads=6), resolver
    )

    assert projection["lineage_path"] == str(lineage.resolve())
    assert projection["lineage_name"] == lineage.name
    assert projection["threads"] == 6
    assert resolve_ncbi_reference_dir(resolver) == str(ncbi.resolve())


def test_species_name_selects_configured_busco_lineage(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(
        tmp_path / "busco", lineage_name="actinopterygii_odb10"
    )
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_species_reference(
        ncbi,
        species_name="Solea ovata",
        lineage_name="actinopterygii_odb10",
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(
        _run_config(threads=6),
        resolver,
        species_name="Solea ovata",
    )

    assert projection["lineage_name"] == "actinopterygii_odb10"
    assert projection["threads"] == 6


def test_species_name_selects_one_of_multiple_ncbi_references(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    resolver, metazoa, viridiplantae = _multi_reference_resolver(tmp_path, lineage)

    assert resolve_ncbi_reference(resolver, species_name="Solea ovata") == (
        "metazoa",
        str(metazoa.resolve()),
    )
    assert resolve_ncbi_reference(resolver, species_name="Arabidopsis thaliana") == (
        "viridiplantae",
        str(viridiplantae.resolve()),
    )


def test_host_check_without_species_validates_multiple_ncbi_references(
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    resolver, metazoa, _viridiplantae = _multi_reference_resolver(tmp_path, lineage)

    assert resolve_ncbi_reference(resolver, species_name=None) == (
        "metazoa",
        str(metazoa.resolve()),
    )


def test_species_name_does_not_require_one_snapshot_match(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi_root = tmp_path / "ncbi"
    metazoa = ncbi_root / "metazoa"
    viridiplantae = ncbi_root / "viridiplantae"
    metazoa.mkdir(parents=True)
    viridiplantae.mkdir(parents=True)
    for reference, prefix in (
        (metazoa, "Metazoa"),
        (viridiplantae, "Viridiplantae"),
    ):
        _write_species_reference(
            reference,
            species_name="Duplicated species",
            lineage_name=lineage.name,
            prefix=prefix,
        )
    config = _config(lineage, metazoa)
    config["databases"].pop("ncbi_metazoa")
    config["databases"]["ncbi"] = {"reference_dir": str(ncbi_root)}
    resolver = EnvResolver(config_data=config)

    assert resolve_ncbi_reference(resolver, species_name="Duplicated species") == (
        "metazoa",
        str(metazoa.resolve()),
    )


def test_species_name_keeps_related_species_fallback_across_snapshots(
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    resolver, metazoa, _viridiplantae = _multi_reference_resolver(tmp_path, lineage)

    assert resolve_ncbi_reference(resolver, species_name="Solea senegalensis") == (
        "metazoa",
        str(metazoa.resolve()),
    )


def test_unresolved_species_error_points_to_manifest_field(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_species_reference(
        ncbi,
        species_name="Fixture species",
        lineage_name=lineage.name,
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    with pytest.raises(
        DatabaseResolutionError,
        match="input_manifest.user_metadata.target_species",
    ):
        resolve_busco_parameters(
            _run_config(),
            resolver,
            species_name="Absent species",
        )


@pytest.mark.parametrize(
    "forbidden_field",
    ("lineage_path", "lineage_name", "lineage_version", "lineage_source"),
)
def test_run_config_rejects_host_database_identity_fields(
    forbidden_field: str,
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    with pytest.raises(ValidationError):
        _run_config(lineage=lineage.name, threads=4, **{forbidden_field: "host"})


def test_database_paths_are_relative_to_env_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "host"
    config_dir.mkdir()
    lineage = ensure_busco_lineage(config_dir / "db" / "busco")
    ncbi = config_dir / "db" / "ncbi"
    ncbi.mkdir(parents=True)
    _write_reference_files(ncbi)
    envs = config_dir / "envs.yaml"
    envs.write_text(
        "default:\n  fallback_env: tools\n"
        "databases:\n"
        "  busco:\n"
        f"    default_lineage: {lineage.name}\n"
        "    lineages:\n"
        f"      {lineage.name}: {{path: db/busco/{lineage.name}, "
        "version: odb10, source: test}\n"
        "  ncbi_metazoa:\n    reference_dir: db/ncbi\n",
        encoding="utf-8",
    )
    resolver = EnvResolver(envs)

    assert resolver.databases is not None
    assert resolver.databases.busco.lineages[lineage.name].path == str(
        lineage.resolve()
    )
    assert resolver.databases.ncbi_metazoa.reference_dir == str(ncbi.resolve())
    assert resolve_ncbi_reference_dir(resolver) == str(ncbi.resolve())


def test_frozen_ncbi_snapshot_identity_rejects_replacement(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_files(ncbi)
    envs = tmp_path / "envs.yaml"
    envs.write_text(
        "default:\n  fallback_env: tools\n"
        "databases:\n"
        f"  busco:\n    default_lineage: {lineage.name}\n"
        f"    lineages:\n      {lineage.name}: {{path: {lineage}, "
        "version: odb10, source: test}\n"
        f"  ncbi_metazoa:\n    reference_dir: {ncbi}\n",
        encoding="utf-8",
    )
    snapshot = capture_dependency_snapshot(config_path=envs)
    frozen = FrozenDependencyResolver(snapshot)
    assert resolve_ncbi_reference_dir(frozen) == str(ncbi.resolve())

    changed = "accession\tchanged\n"
    (ncbi / "Metazoa.ncbi.tsv").write_text(changed, encoding="utf-8")

    with pytest.raises(
        DatabaseResolutionError, match="changed after dependency freeze"
    ):
        resolve_ncbi_reference_dir(frozen)


def test_ncbi_snapshot_requires_one_same_prefix_four_file_set(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_files(ncbi)
    resolver = EnvResolver(config_data=_config(lineage, ncbi))
    (ncbi / "Metazoa.ncbi.lineage2").rename(ncbi / "Viridiplantae.ncbi.lineage2")

    with pytest.raises(DatabaseResolutionError, match="four_file_set_invalid"):
        resolve_ncbi_reference_dir(resolver)
