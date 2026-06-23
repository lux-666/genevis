"""Build bounded related-species evidence from local NCBI Metazoa exports."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


REFERENCE_DIR = Path(__file__).resolve().parents[2] / "data" / "reference" / "ncbi_metazoa"
REFERENCE_MANIFEST_FILENAME = "reference_manifest.json"
MAX_SELECTED_RECORDS = 5

_ACCESSION_RE = re.compile(r"^(GC[AF])_(\d+)(?:\.\d+)?$", re.IGNORECASE)
_ALTERNATE_NAME_RE = re.compile(
    r"(?:\balternate\b|\balt(?:ernative)?\b|\.alt\b|"
    r"(?:^|[._ -])hap(?:lotype)?[._ -]?[12](?=$|[._ -])|"
    r"\b(?:maternal|paternal)(?:[._ -]+haplotype)?\b)",
    re.IGNORECASE,
)
_ALTERNATE_METADATA_RE = re.compile(
    r"(?:alternate|maternal|paternal|hap(?:lotype)?[._ -]?[12])",
    re.IGNORECASE,
)
_SPECIES_KEYS = ("target_species", "species_name", "scientific_name", "organism", "species", "chinese_name")


def build_related_species_evidence_pack(
    species_name: str | None,
    *,
    reference_dir: Path | str = REFERENCE_DIR,
) -> dict[str, Any]:
    """Resolve a target and return non-blocking, locally sourced evidence.

    All failure paths return a valid, explainable pack; callers should never
    need to suppress an exception simply because optional reference data are
    absent or incomplete.
    """
    reference_dir = Path(reference_dir)
    sources = {
        "tsv": str(reference_dir / "Metazoa.ncbi.tsv"),
        "lineage": str(reference_dir / "Metazoa.ncbi.lineage"),
        "alias": str(reference_dir / "species_alias.tsv"),
    }
    base = _empty_pack(species_name, sources)
    if not species_name or not str(species_name).strip():
        base["notes"].append("target_species_not_provided")
        return base
    provenance, provenance_error = _reference_provenance(reference_dir, sources)
    base["source"] = provenance
    if provenance_error:
        base["notes"].append(provenance_error)
        return base

    try:
        aliases = _load_aliases(Path(sources["alias"]))
        rows = _load_tsv(Path(sources["tsv"]))
        lineage_by_taxid = _load_lineage(Path(sources["lineage"]))
    except OSError as exc:
        base["notes"].append(f"reference_source_unavailable:{exc.filename or exc}")
        return base
    except (csv.Error, UnicodeDecodeError, ValueError) as exc:
        base["notes"].append(f"reference_source_unreadable:{type(exc).__name__}")
        return base

    target = _resolve_target(str(species_name), aliases)
    base["target"] = target
    canonical = target["canonical_scientific_name"]
    if not canonical:
        base["notes"].append("target_species_unresolved")
        return base

    exact = _select_evidence(
        [row for row in rows if _normalise(row.get("Organism Name")) == _normalise(canonical)]
    )
    if exact["selected_records"]:
        return _complete_pack(base, "exact", exact)

    genus = target["taxonomic_hints"].get("genus") or canonical.split()[0]
    same_genus = _select_evidence(
        [row for row in rows if _organism_genus(row) == _normalise(genus)]
    )
    if same_genus["selected_records"]:
        return _complete_pack(base, "same_genus", same_genus)

    family = target["taxonomic_hints"].get("family")
    if family:
        same_family = _select_evidence(_rows_with_lineage_term(rows, lineage_by_taxid, family))
        if same_family["selected_records"]:
            return _complete_pack(base, "same_family", same_family)
    else:
        base["notes"].append("same_family_unavailable_without_taxonomic_hint")

    order = target["taxonomic_hints"].get("order")
    if order:
        same_order = _select_evidence(_rows_with_lineage_term(rows, lineage_by_taxid, order))
        if same_order["selected_records"]:
            return _complete_pack(base, "same_order", same_order)
    else:
        base["notes"].append("same_order_unavailable_without_taxonomic_hint")

    base["notes"].append("no_useful_related_species_assemblies_found")
    return base


def species_name_from_metadata(metadata: dict[str, Any]) -> str | None:
    """Return the first supported target-species value from manifest metadata."""
    for key in _SPECIES_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("name", "scientific_name", "chinese_name"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return None


def _empty_pack(species_name: str | None, sources: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "unavailable",
        "target": {
            "input_name": species_name,
            "canonical_scientific_name": None,
            "resolution": "unresolved",
            "taxonomic_hints": {},
        },
        "matching_mode": "unavailable",
        "selected_records": [],
        "excluded_alternate_haplotype_records": [],
        "computed_summary": _computed_summary([]),
        "source": sources,
        "notes": [],
    }


def _complete_pack(base: dict[str, Any], matching_mode: str, selection: dict[str, Any]) -> dict[str, Any]:
    return {
        **base,
        "status": "available",
        "matching_mode": matching_mode,
        "selected_records": selection["selected_records"],
        "excluded_alternate_haplotype_records": selection["excluded_alternate_haplotype_records"],
        "computed_summary": _computed_summary(selection["selected_records"]),
        "notes": [
            *base["notes"],
            "related_species_evidence_is_comparative_only",
            "alternate_haplotype_assemblies_are_not_primary_evidence",
        ],
    }


def _load_aliases(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise OSError(2, "species alias table not found", str(path))
    return _load_tsv(path)


def _reference_provenance(
    reference_dir: Path,
    sources: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    """Load and verify the tracked snapshot manifest before using its data."""
    manifest_path = reference_dir / REFERENCE_MANIFEST_FILENAME
    provenance: dict[str, Any] = {**sources, "manifest": str(manifest_path), "verification": "unverified"}
    if not manifest_path.exists():
        return provenance, "reference_version_manifest_missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return provenance, "reference_version_manifest_unreadable"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        return provenance, "reference_version_manifest_invalid"

    provenance.update({
        "dataset_id": manifest.get("dataset_id"),
        "source_provider": manifest.get("source_provider"),
        "source_version": manifest.get("source_version"),
        "exported_at": manifest.get("exported_at"),
        "files": {},
    })
    mismatches: list[str] = []
    for name, expected in manifest["files"].items():
        if not isinstance(expected, dict):
            return provenance, "reference_version_manifest_invalid"
        path = reference_dir / name
        actual = {"path": str(path), "sha256": _file_sha256(path) if path.exists() else None, "size_bytes": path.stat().st_size if path.exists() else None}
        provenance["files"][name] = actual
        if actual["sha256"] != expected.get("sha256") or actual["size_bytes"] != expected.get("size_bytes"):
            mismatches.append(name)
    if mismatches:
        provenance["verification"] = "mismatch"
        provenance["mismatched_files"] = mismatches
        return provenance, f"reference_version_mismatch:{','.join(mismatches)}"
    provenance["verification"] = "verified"
    return provenance, None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _load_lineage(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        raise OSError(2, "NCBI lineage table not found", str(path))
    result: dict[str, set[str]] = {}
    for row in _load_tsv(path):
        taxid = str(row.get("Organism Taxonomic ID") or "").strip()
        lineage = str(row.get("Lineage") or "")
        if taxid:
            result[taxid] = {_normalise(term) for term in lineage.split(";") if term.strip()}
    return result


def _resolve_target(species_name: str, aliases: list[dict[str, str]]) -> dict[str, Any]:
    query = _normalise(species_name)
    for row in aliases:
        canonical = _value(row, "canonical_scientific_name", "scientific_name")
        aliases_text = _value(row, "aliases", "alias")
        names = [
            _value(row, "chinese_name", "中文名"),
            canonical,
            *re.split(r"[;,|]", aliases_text or ""),
        ]
        if any(query == _normalise(name) for name in names if name):
            return {
                "input_name": species_name,
                "canonical_scientific_name": canonical or species_name,
                "resolution": "alias_table",
                "taxonomic_hints": {
                    key: value
                    for key in ("genus", "family", "order")
                    if (value := _value(row, key))
                },
            }
    tokens = species_name.split()
    return {
        "input_name": species_name,
        "canonical_scientific_name": species_name if len(tokens) >= 2 else None,
        "resolution": "scientific_name_input" if len(tokens) >= 2 else "unresolved",
        "taxonomic_hints": {"genus": tokens[0]} if len(tokens) >= 2 else {},
    }


def _select_evidence(rows: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    condensed = [_condense_record(row) for row in rows]
    primary = _deduplicate([record for record in condensed if not record["is_alternate_haplotype"]])
    alternates = _deduplicate([record for record in condensed if record["is_alternate_haplotype"]])
    return {
        "selected_records": primary[:MAX_SELECTED_RECORDS],
        "excluded_alternate_haplotype_records": alternates[:MAX_SELECTED_RECORDS],
    }


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=_record_sort_key):
        selected.setdefault(_accession_numeric_core(record["accession"]), record)
    return sorted(selected.values(), key=_record_sort_key)


def _record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["is_alternate_haplotype"],
        not record["accession"].upper().startswith("GCF_"),
        -(record["scaffold_n50_bp"] or -1),
        -(record["contig_n50_bp"] or -1),
        record["scaffold_count"] if record["scaffold_count"] is not None else float("inf"),
        record["contig_count"] if record["contig_count"] is not None else float("inf"),
        record["accession"],
    )


def _condense_record(row: dict[str, str]) -> dict[str, Any]:
    accession = _value(row, "Current Accession", "Assembly Accession") or "unknown"
    assembly_name = _value(row, "Assembly Name")
    alternate_reason = _alternate_haplotype_reason(row, assembly_name)
    return {
        "accession": accession,
        "assembly_name": assembly_name or None,
        "organism_name": _value(row, "Organism Name") or None,
        "taxid": _value(row, "Organism Taxonomic ID") or None,
        "assembly_method": _value(row, "Assembly Assembly Method") or None,
        "total_sequence_length_bp": _integer(_value(row, "Assembly Stats Total Sequence Length")),
        "chromosome_count": _integer(_value(row, "Assembly Stats Total Number of Chromosomes")),
        "contig_count": _integer(_value(row, "Assembly Stats Number of Contigs")),
        "scaffold_count": _integer(_value(row, "Assembly Stats Number of Scaffolds")),
        "contig_n50_bp": _integer(_value(row, "Assembly Stats Contig N50")),
        "scaffold_n50_bp": _integer(_value(row, "Assembly Stats Scaffold N50")),
        "gc_percent": _number(_value(row, "Assembly Stats GC Percent")),
        "busco": _busco(row),
        "is_alternate_haplotype": alternate_reason is not None,
        "alternate_haplotype_reason": alternate_reason,
    }


def _busco(row: dict[str, str]) -> dict[str, Any] | None:
    values = {
        "lineage": _value(row, "Annotation BUSCO Lineage") or None,
        "complete": _number(_value(row, "Annotation BUSCO Complete")),
        "single_copy": _number(_value(row, "Annotation BUSCO Single Copy")),
        "duplicated": _number(_value(row, "Annotation BUSCO Duplicated")),
        "fragmented": _number(_value(row, "Annotation BUSCO Fragmented")),
        "missing": _number(_value(row, "Annotation BUSCO Missing")),
        "total_count": _integer(_value(row, "Annotation BUSCO Total Count")),
        "version": _value(row, "Annotation BUSCO Version") or None,
    }
    return values if any(value is not None for value in values.values()) else None


def _alternate_haplotype_reason(row: dict[str, str], assembly_name: str) -> str | None:
    metadata = _value(
        row,
        "Assembly Type",
        "Assembly Info Assembly Type",
        "Assembly Info Diploid Role",
        "Diploid Role",
    )
    if _ALTERNATE_METADATA_RE.search(metadata):
        return "assembly_metadata"
    if _ALTERNATE_NAME_RE.search(assembly_name or ""):
        return "assembly_name"
    return None


def _computed_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"selected_record_count": len(records)}
    for field in (
        "total_sequence_length_bp", "chromosome_count", "contig_count", "scaffold_count",
        "contig_n50_bp", "scaffold_n50_bp", "gc_percent",
    ):
        values = [record[field] for record in records if record.get(field) is not None]
        if values:
            summary[field] = {"min": min(values), "median": median(values), "max": max(values)}
    chromosome_values = [record["chromosome_count"] for record in records if record.get("chromosome_count") is not None]
    if chromosome_values:
        counts = Counter(chromosome_values)
        summary["chromosome_count"]["mode"] = min(value for value, count in counts.items() if count == max(counts.values()))
    busco_lineages = sorted({
        record["busco"]["lineage"]
        for record in records
        if record.get("busco") and record["busco"].get("lineage")
    })
    if busco_lineages:
        summary["busco_lineages"] = busco_lineages
    return summary


def _rows_with_lineage_term(rows: list[dict[str, str]], lineage_by_taxid: dict[str, set[str]], term: str) -> list[dict[str, str]]:
    wanted = _normalise(term)
    return [
        row for row in rows
        if wanted in lineage_by_taxid.get(str(row.get("Organism Taxonomic ID") or "").strip(), set())
    ]


def _organism_genus(row: dict[str, str]) -> str:
    organism = _value(row, "Organism Name")
    return _normalise(organism.split()[0]) if organism else ""


def _accession_numeric_core(accession: str) -> str:
    match = _ACCESSION_RE.match(accession)
    return match.group(2) if match else accession.upper()


def _value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _integer(value: str) -> int | None:
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def _number(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _normalise(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())
