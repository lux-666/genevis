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
from app.reference.ncbi import (
    MATCHING_TIER_RANK,
    MATCHING_TIERS,
    resolve_busco_lineage_for_species,
    validate_reference_snapshot,
)
from app.schemas.run_config import RunConfig
from tests.busco_fixture import ensure_busco_lineage


def _write_reference(reference: Path, files: dict[str, str]) -> None:
    tsv_name = next(name for name in files if name.endswith(".ncbi.tsv"))
    prefix = tsv_name.removesuffix(".ncbi.tsv")
    lineage = files[f"{prefix}.ncbi.lineage"]
    native = {
        f"{prefix}.ncbi.json": "{}\n",
        f"{prefix}.ncbi.lineage": lineage,
        f"{prefix}.ncbi.lineage2": files.get(f"{prefix}.ncbi.lineage2", lineage),
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
    lineage_name: str | None,
    prefix: str = "Metazoa",
    full_lineage: str | None = None,
    taxonomy_lineage: str | None = None,
) -> None:
    _write_reference(
        reference,
        {
            f"{prefix}.ncbi.tsv": (
                "Current Accession\tOrganism Name\tOrganism Taxonomic ID\t"
                "Annotation BUSCO Lineage\n"
                f"GCF_000000001.1\t{species_name}\t1\t{lineage_name or ''}\n"
            ),
            f"{prefix}.ncbi.lineage": (
                "Organism Taxonomic ID\tLineage\n"
                f"1\t{full_lineage or f'{prefix};{species_name}'}\n"
            ),
            f"{prefix}.ncbi.lineage2": (
                "Organism Taxonomic ID\tLineage\n"
                f"1\t{taxonomy_lineage or f'{prefix};{species_name}'}\n"
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
            "ncbi": {"reference_dir": str(ncbi)},
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
    return EnvResolver(config_data=config), metazoa, viridiplantae


def test_matching_tier_order_has_one_canonical_contract() -> None:
    assert MATCHING_TIERS == ("exact", "same_genus", "same_family", "same_order")
    assert MATCHING_TIER_RANK == {
        tier: rank for rank, tier in enumerate(MATCHING_TIERS)
    }


def test_reference_snapshot_accepts_taxids_without_lineage(tmp_path: Path) -> None:
    reference = tmp_path / "ncbi"
    reference.mkdir()
    _write_reference(
        reference,
        {
            "Metazoa.ncbi.tsv": "accession\n",
            "Metazoa.ncbi.lineage": (
                "Organism Taxonomic ID\tLineage\n"
                "1\t\n"
                "2\tcellular organisms;Eukaryota;Metazoa\n"
            ),
        },
    )

    assert validate_reference_snapshot(reference)["verification"] == "verified"


def test_reference_snapshot_rejects_non_numeric_taxid(tmp_path: Path) -> None:
    reference = tmp_path / "ncbi"
    reference.mkdir()
    _write_reference(
        reference,
        {
            "Metazoa.ncbi.tsv": "accession\n",
            "Metazoa.ncbi.lineage": "Organism Taxonomic ID\tLineage\nbad\t\n",
        },
    )

    with pytest.raises(ValueError, match="reference_source_unreadable"):
        validate_reference_snapshot(reference)


def test_logical_lineage_resolves_to_complete_projection(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_files(ncbi)
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(_run_config(threads=6), resolver)

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


@pytest.mark.parametrize(
    "target,reference_species,lineage_name,taxonomy",
    [
        (
            "Pyricularia oryzae",
            "Pyricularia oryzae",
            "fungi_odb10",
            "Eukaryota;Fungi;Ascomycota;Pyricularia;Pyricularia oryzae",
        ),
        (
            "Pyricularia oryzae",
            "Pyricularia grisea",
            "fungi_odb10",
            "Eukaryota;Fungi;Ascomycota;Pyricularia;Pyricularia grisea",
        ),
        (
            "Solea ovata",
            "Solea ovata",
            "metazoa_odb10",
            "Eukaryota;Metazoa;Chordata;Solea;Solea ovata",
        ),
    ],
)
def test_unannotated_species_uses_verified_taxonomy_and_installed_dataset(
    tmp_path, target, reference_species, lineage_name, taxonomy
):
    lineage = ensure_busco_lineage(tmp_path / "busco", lineage_name=lineage_name)
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_species_reference(
        ncbi,
        species_name=reference_species,
        lineage_name=None,
        prefix="Snapshot",
        full_lineage=taxonomy,
    )
    unrelated = ensure_busco_lineage(tmp_path / "busco")
    config = _config(unrelated, ncbi)
    config["databases"]["busco"]["lineages"].update(
        _config(lineage, ncbi)["databases"]["busco"]["lineages"]
    )
    projection = resolve_busco_parameters(
        _run_config(), EnvResolver(config_data=config), species_name=target
    )
    assert projection["lineage_name"] == lineage_name


@pytest.mark.parametrize(
    "observed,full_lineage,configured",
    [
        (
            None,
            "Eukaryota;Fungi;Ascomycota;Pyricularia;Pyricularia oryzae",
            {"fungi_odb10", "ascomycota_odb10"},
        ),
        (
            None,
            "Eukaryota;Fungi;Pyricularia;Pyricularia oryzae",
            {"fungi_odb10", "fungi_odb12"},
        ),
        (None, "Eukaryota;Pyricularia;Pyricularia oryzae", {"fungi_odb10"}),
        (None, "Eukaryota;Fungi;Pyricularia;Pyricularia oryzae", {"metazoa_odb10"}),
        (
            "ascomycota_odb10",
            "Eukaryota;Fungi;Pyricularia;Pyricularia oryzae",
            {"fungi_odb10"},
        ),
    ],
)
def test_taxonomy_fallback_rejects_ambiguity_missing_or_conflicting_evidence(
    tmp_path, observed, full_lineage, configured
):
    _write_species_reference(
        tmp_path,
        species_name="Pyricularia oryzae",
        lineage_name=observed,
        prefix="Fungi",
        full_lineage=full_lineage,
        taxonomy_lineage="Fungi;Pyricularia;Pyricularia oryzae",
    )
    with pytest.raises(ValueError, match="cannot resolve one configured BUSCO lineage"):
        resolve_busco_lineage_for_species(
            "Pyricularia oryzae",
            reference_dir=tmp_path,
            configured_lineages=configured,
        )


def test_plant_genus_taxonomy_selects_fixed_lineage_without_exact_target(
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    species_name = "Brassica rapa subsp. pekinensis"
    _write_species_reference(
        ncbi,
        species_name="Brassica rapa",
        lineage_name=None,
        prefix="Viridiplantae",
        taxonomy_lineage=("Brassicales;Brassicaceae;Brassica;Brassica rapa"),
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(
        _run_config(threads=6),
        resolver,
        species_name=species_name,
    )

    assert projection["lineage_name"] == "embryophyta_odb10"


def test_plant_taxonomy_fixed_lineage_precedes_narrower_annotation(
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    species_name = "Brassica rapa subsp. pekinensis"
    _write_species_reference(
        ncbi,
        species_name=species_name,
        lineage_name="brassicales_odb10",
        prefix="Viridiplantae",
        taxonomy_lineage=f"Viridiplantae;Embryophyta;Brassica;{species_name}",
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(
        _run_config(),
        resolver,
        species_name=species_name,
    )

    assert projection["lineage_name"] == "embryophyta_odb10"


def test_nonplant_taxonomy_cannot_fall_back_to_embryophyta(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_species_reference(
        ncbi,
        species_name="Solea ovata",
        lineage_name=None,
        prefix="Viridiplantae",
        full_lineage="Eukaryota;Metazoa;Chordata;Solea;Solea ovata",
        taxonomy_lineage="Eukaryota;Metazoa;Chordata;Solea;Solea ovata",
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    with pytest.raises(
        DatabaseResolutionError,
        match="cannot resolve one configured BUSCO lineage",
    ):
        resolve_busco_parameters(
            _run_config(),
            resolver,
            species_name="Solea ovata",
        )


def test_plant_taxonomy_requires_fixed_lineage_to_be_installed(
    tmp_path: Path,
) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco", lineage_name="metazoa_odb10")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    species_name = "Brassica rapa subsp. pekinensis"
    _write_species_reference(
        ncbi,
        species_name=species_name,
        lineage_name="metazoa_odb10",
        prefix="Viridiplantae",
        taxonomy_lineage=f"Viridiplantae;Embryophyta;Brassica;{species_name}",
    )
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    with pytest.raises(
        DatabaseResolutionError,
        match="plant taxonomy.*requires installed BUSCO lineage 'embryophyta_odb10'",
    ):
        resolve_busco_parameters(
            _run_config(),
            resolver,
            species_name=species_name,
        )


def test_run_config_rejects_a_database_lineage_selector() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        _run_config(lineage="metazoa_odb10")


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
    with pytest.raises(ValidationError):
        _run_config(threads=4, **{forbidden_field: "host"})


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
        "  ncbi:\n    reference_dir: db/ncbi\n",
        encoding="utf-8",
    )
    resolver = EnvResolver(envs)

    assert resolver.databases is not None
    assert resolver.databases.busco.lineages[lineage.name].path == str(
        lineage.resolve()
    )
    assert resolver.databases.ncbi.reference_dir == str(ncbi.resolve())
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
        f"  ncbi:\n    reference_dir: {ncbi}\n",
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


def test_busco_stage_requires_frozen_database_even_with_complete_cursor(tmp_path):
    from app.stages.assembly_review.method_application.busco import busco_parameters

    lineage = ensure_busco_lineage(tmp_path)
    parameters = {
        "lineage_path": str(lineage.resolve()),
        "lineage_name": lineage.name,
        "lineage_version": "odb10",
        "lineage_source": "test_fixture",
        "threads": 1,
        "mode": "genome",
    }
    values, error = busco_parameters(
        {"data_dir": str(tmp_path / "runs" / "uncommitted")},
        {"busco_parameters": parameters},
    )
    assert values == {}
    assert error
