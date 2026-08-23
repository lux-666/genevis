"""Explicit BUSCO configuration helpers for unrelated test fixtures."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

_BUSCO_PROFILE_ID = "1001at9999"


def busco_parameters(lineage_path: str | Path) -> dict[str, object]:
    """Return the host-independent BUSCO RunConfig projection.

    The database registry owns paths, versions, and provenance.  Tests pass a
    path here only because they create the fixture on disk; the resulting
    payload must contain the logical lineage selector and execution threads.
    """

    lineage_name = Path(lineage_path).name
    return {
        "lineage": lineage_name,
        "threads": 4,
    }


def assembly_review_config(lineage_path: str | Path) -> dict[str, object]:
    return {"busco": busco_parameters(lineage_path)}


def assembly_review_selector_config(
    lineage: str = "embryophyta_odb10",
) -> dict[str, object]:
    """Return the host-independent Assembly Review selector projection.

    Database paths belong to the host ``envs.yaml`` registry.  This helper is
    kept for tests that only need the selector projection without creating a
    local database directory.
    """

    return {"busco": {"lineage": lineage}}


def ensure_busco_lineage(
    root: str | Path,
    *,
    lineage_name: str = "embryophyta_odb10",
) -> Path:
    lineage = Path(root) / lineage_name
    lineage.mkdir(parents=True, exist_ok=True)
    (lineage / "dataset.cfg").write_text(
        f"name={lineage_name}\n"
        "species=arabidopsis\n"
        "domain=eukaryota\n"
        "creation_date=2024-01-08\n"
        "number_of_BUSCOs=1\n"
        "number_of_species=1\n",
        encoding="utf-8",
    )
    hmms = lineage / "hmms"
    hmms.mkdir(exist_ok=True)
    (hmms / f"{_BUSCO_PROFILE_ID}.hmm").write_text(
        "HMMER3/f [3.3.2 | Nov 2020]\n"
        f"NAME  {_BUSCO_PROFILE_ID}\n"
        "LENG  1\n"
        "ALPH  amino\n"
        "HMM          A\n"
        "//\n",
        encoding="ascii",
    )
    (lineage / "scores_cutoff").write_text(
        f"{_BUSCO_PROFILE_ID} 42.0\n",
        encoding="utf-8",
    )
    if not lineage_name.endswith("_odb12"):
        (lineage / "lengths_cutoff").write_text(
            f"{_BUSCO_PROFILE_ID} 1 0 1\n",
            encoding="utf-8",
        )
    (lineage / "ancestral").write_text(
        f">{_BUSCO_PROFILE_ID}\nM\n",
        encoding="utf-8",
    )
    with gzip.open(lineage / "refseq_db.faa.gz", "wt", encoding="utf-8") as handle:
        handle.write(f">{_BUSCO_PROFILE_ID}\nM\n")
    return lineage


def ensure_test_host_envs(
    root: str | Path,
    *,
    lineage_path: str | Path | None = None,
) -> Path:
    """Create a complete, self-contained host envs fixture for freeze tests.

    The fixture deliberately includes both database families because the
    freeze contract validates the complete host registry whenever a review
    stage is requested.  It is written next to the temporary run and callers
    must pass the returned path as ``freeze_session_inputs(config_path=...)``.
    """

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    lineage = (
        Path(lineage_path)
        if lineage_path is not None
        else ensure_busco_lineage(root_path / "busco")
    )
    reference = root_path / "ncbi_metazoa"
    reference.mkdir(parents=True, exist_ok=True)
    source_contents = {
        "Metazoa.ncbi.tsv": "accession\nfixture\n",
        "Metazoa.ncbi.lineage": "taxid\tlineage\nfixture\tfixture\n",
        "species_alias.tsv": "alias\tscientific_name\nfixture\tFixture species\n",
    }
    files: dict[str, dict[str, int | str]] = {}
    for name, content in source_contents.items():
        payload = content.encode("utf-8")
        (reference / name).write_bytes(payload)
        files[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    (reference / "reference_manifest.json").write_text(
        json.dumps({"files": files}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path = root_path / "envs.yaml"
    config_path.write_text(
        json.dumps(
            {
                "default": {"fallback_env": "test-env"},
                "stages": {},
                "methods": {},
                "packages": {},
                "databases": {
                    "busco": {
                        "default_lineage": "embryophyta_odb10",
                        "lineages": {
                            "embryophyta_odb10": {
                                "path": str(lineage),
                                "version": "odb10",
                                "source": "test_fixture",
                            }
                        },
                    },
                    "ncbi_metazoa": {"reference_dir": str(reference)},
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def add_busco_config(
    payload: dict[str, Any],
    lineage_path: str | Path,
) -> dict[str, Any]:
    return {
        **payload,
        "assembly_review": assembly_review_config(lineage_path),
    }
