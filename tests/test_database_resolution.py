from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.execution.database_resolution import (
    DatabaseResolutionError,
    resolve_busco_parameters,
    resolve_ncbi_reference_dir,
)
from app.execution.dependency_snapshot import (
    FrozenDependencyResolver,
    capture_dependency_snapshot,
)
from app.execution.env_resolver import EnvResolver
from app.schemas.run_config import RunConfig
from tests.busco_fixture import ensure_busco_lineage


def _write_reference_manifest(reference: Path) -> None:
    files = {
        "Metazoa.ncbi.tsv": "accession\n",
        "Metazoa.ncbi.lineage": "taxid\tlineage\n",
        "species_alias.tsv": "alias\tscientific_name\n",
    }
    entries = {}
    for name, content in files.items():
        (reference / name).write_text(content, encoding="utf-8")
        payload = content.encode()
        entries[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    (reference / "reference_manifest.json").write_text(
        json.dumps({"files": entries}, sort_keys=True) + "\n", encoding="utf-8"
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


def test_logical_lineage_resolves_to_complete_projection(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_manifest(ncbi)
    resolver = EnvResolver(config_data=_config(lineage, ncbi))

    projection = resolve_busco_parameters(
        _run_config(lineage=lineage.name, threads=6), resolver
    )

    assert projection["lineage_path"] == str(lineage.resolve())
    assert projection["lineage_name"] == lineage.name
    assert projection["threads"] == 6
    assert resolve_ncbi_reference_dir(resolver) == str(ncbi.resolve())


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
    _write_reference_manifest(ncbi)
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


def test_frozen_ncbi_manifest_identity_rejects_replacement(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_manifest(ncbi)
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
    entries = {}
    for name in ("Metazoa.ncbi.tsv", "Metazoa.ncbi.lineage", "species_alias.tsv"):
        content = (ncbi / name).read_bytes()
        entries[name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    (ncbi / "reference_manifest.json").write_text(
        json.dumps({"files": entries}, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(
        DatabaseResolutionError, match="changed after dependency freeze"
    ):
        resolve_ncbi_reference_dir(frozen)


def test_ncbi_manifest_requires_exact_files_and_hash_metadata(tmp_path: Path) -> None:
    lineage = ensure_busco_lineage(tmp_path / "busco")
    ncbi = tmp_path / "ncbi"
    ncbi.mkdir()
    _write_reference_manifest(ncbi)
    resolver = EnvResolver(config_data=_config(lineage, ncbi))
    manifest_path = ncbi / "reference_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["unexpected.txt"] = {
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "size_bytes": 1,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatabaseResolutionError, match="whitelist"):
        resolve_ncbi_reference_dir(resolver)
