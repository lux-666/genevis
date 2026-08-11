"""Explicit BUSCO configuration helpers for unrelated test fixtures."""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import Any

_BUSCO_PROFILE_ID = "1001at9999"


def busco_parameters(lineage_path: str | Path) -> dict[str, object]:
    lineage_name = Path(lineage_path).name
    version_match = re.search(r"(odb\d+(?:\.\d+)?)$", lineage_name)
    if version_match is None:
        raise ValueError(
            f"test BUSCO lineage name lacks an odb version: {lineage_name}"
        )
    return {
        "lineage_path": str(lineage_path),
        "lineage_name": lineage_name,
        "lineage_version": version_match.group(1),
        "lineage_source": "test_fixture",
        "threads": 4,
    }


def assembly_review_config(lineage_path: str | Path) -> dict[str, object]:
    return {"busco": busco_parameters(lineage_path)}


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


def add_busco_config(
    payload: dict[str, Any],
    lineage_path: str | Path,
) -> dict[str, Any]:
    return {
        **payload,
        "assembly_review": assembly_review_config(lineage_path),
    }
