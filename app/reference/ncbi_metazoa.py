"""Build bounded related-species evidence from local NCBI Metazoa exports."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import median, quantiles
from typing import Any


REFERENCE_DIR = Path(__file__).resolve().parents[2] / "data" / "reference" / "ncbi_metazoa"
REFERENCE_MANIFEST_FILENAME = "reference_manifest.json"
MAX_SELECTED_RECORDS = 5
DISPLAY_COHORT_SIZE = 25
ANALYSIS_COHORT_SIZE = 150

_STATISTICS_TIER_RANK = {"exact": 0, "same_genus": 1, "same_family": 2, "same_order": 3}
_STATISTICS_BASIS = {
    "n50": "contig_n50_bp",
    "assembly_size": "total_sequence_length_bp",
    "gc": "gc_percent",
    "busco_complete": "busco_complete_percent",
    "busco_duplicated": "busco_duplicated_percent",
}

_ACCESSION_RE = re.compile(r"^(GC[AF])_(\d+)(?:\.\d+)?$", re.IGNORECASE)
_ALTERNATE_NAME_RE = re.compile(
    r"(?:\balternate\b|\balt(?:ernative)?\b|\.alt\b|"
    r"(?:^|[._ -])hap(?:lotype)?[._ -]?[12](?=$|[._ -])|"
    r"\b(?:maternal|paternal)(?:[._ -]+haplotype)?\b)",
    re.IGNORECASE,
)
_ALTERNATE_METADATA_RE = re.compile(
    r"\b(?:alternate|maternal|paternal|hap[._ -]?[12])\b",
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


def build_related_species_statistics_pack(
    species_name: str | None,
    *,
    reference_dir: Path | str = REFERENCE_DIR,
    target_metrics: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Collect cross-tier related-species evidence for comparative statistics.

    Unlike ``build_related_species_evidence_pack``, this collects candidates
    across all four tiers (exact/same_genus/same_family/same_order) instead of
    stopping at the first tier with a hit, so callers can build cohort-level
    statistics and plots. Never raises; failure paths return a valid
    "unavailable" pack.
    """
    reference_dir = Path(reference_dir)
    sources = {
        "tsv": str(reference_dir / "Metazoa.ncbi.tsv"),
        "lineage": str(reference_dir / "Metazoa.ncbi.lineage"),
        "alias": str(reference_dir / "species_alias.tsv"),
    }
    base = _empty_statistics_pack(species_name, sources, target_metrics)
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

    seen_taxids: set[str] = set()
    tier_rows: dict[str, list[dict[str, str]]] = {}

    exact_candidates = [row for row in rows if _normalise(row.get("Organism Name")) == _normalise(canonical)]
    tier_rows["exact"] = _unseen_rows(exact_candidates, seen_taxids)
    _remember_taxids(tier_rows["exact"], seen_taxids)

    genus = target["taxonomic_hints"].get("genus") or canonical.split()[0]
    genus_candidates = [row for row in rows if _organism_genus(row) == _normalise(genus)]
    tier_rows["same_genus"] = _unseen_rows(genus_candidates, seen_taxids)
    _remember_taxids(tier_rows["same_genus"], seen_taxids)

    family = target["taxonomic_hints"].get("family")
    if family:
        family_candidates = _rows_with_lineage_term(rows, lineage_by_taxid, family)
        tier_rows["same_family"] = _unseen_rows(family_candidates, seen_taxids)
        _remember_taxids(tier_rows["same_family"], seen_taxids)
    else:
        tier_rows["same_family"] = []
        base["notes"].append("same_family_unavailable_without_taxonomic_hint")

    order = target["taxonomic_hints"].get("order")
    if order:
        order_candidates = _rows_with_lineage_term(rows, lineage_by_taxid, order)
        tier_rows["same_order"] = _unseen_rows(order_candidates, seen_taxids)
        _remember_taxids(tier_rows["same_order"], seen_taxids)
    else:
        tier_rows["same_order"] = []
        base["notes"].append("same_order_unavailable_without_taxonomic_hint")

    tier_counts = {tier: len(tier_rows[tier]) for tier in ("exact", "same_genus", "same_family", "same_order")}

    condensed: list[dict[str, Any]] = []
    for tier in ("exact", "same_genus", "same_family", "same_order"):
        for row in tier_rows[tier]:
            record = _condense_record(row)
            record["assembly_level"] = _value(row, "Assembly Level", "Assembly Info Assembly Level") or None
            record["tier"] = tier
            condensed.append(record)

    primary = _deduplicate_by_key([r for r in condensed if not r["is_alternate_haplotype"]], _statistics_sort_key)
    alternates = _deduplicate_by_key([r for r in condensed if r["is_alternate_haplotype"]], _statistics_sort_key)

    analysis_records = primary[:ANALYSIS_COHORT_SIZE]

    notes = list(base["notes"])
    if not primary:
        notes.append("no_related_species_assemblies_found_across_any_tier")
    notes.extend([
        "related_species_statistics_is_comparative_only",
        "not_an_automatic_threshold",
        "not_an_assembly_prior",
        "chromosome_count_is_ncbi_assembly_metadata_not_true_karyotype",
    ])

    return {
        **base,
        "status": "available",
        "tier_counts": tier_counts,
        "display_cohort": {
            "size_limit": DISPLAY_COHORT_SIZE,
            "records": primary[:DISPLAY_COHORT_SIZE],
            "excluded_alternate_haplotype_records": alternates[:DISPLAY_COHORT_SIZE],
        },
        "analysis_cohort": {
            "size_limit": ANALYSIS_COHORT_SIZE,
            "records": analysis_records,
            "excluded_alternate_haplotype_records": alternates[:ANALYSIS_COHORT_SIZE],
        },
        "computed_summary": _statistics_computed_summary(analysis_records),
        "target_position": _statistics_target_position(analysis_records, target_metrics),
        "notes": notes,
    }


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
    """Return exclusion reason if this record represents an alternate haplotype assembly."""

    # Check assembly name first (more reliable for alternates)
    if _ALTERNATE_NAME_RE.search(assembly_name or ""):
        return "assembly_name"

    # Check Assembly Type and Diploid Role fields with refined pattern
    metadata = _value(
        row,
        "Assembly Type",
        "Assembly Info Assembly Type",
        "Assembly Info Diploid Role",
        "Diploid Role",
    )
    if _ALTERNATE_METADATA_RE.search(metadata):
        return "assembly_metadata"

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


def _empty_statistics_pack(
    species_name: str | None,
    sources: dict[str, str],
    target_metrics: dict[str, float] | None,
) -> dict[str, Any]:
    empty_cohort = {"size_limit": DISPLAY_COHORT_SIZE, "records": [], "excluded_alternate_haplotype_records": []}
    analysis_cohort = {"size_limit": ANALYSIS_COHORT_SIZE, "records": [], "excluded_alternate_haplotype_records": []}
    return {
        "schema_version": 1,
        "status": "unavailable",
        "target": {
            "input_name": species_name,
            "canonical_scientific_name": None,
            "resolution": "unresolved",
            "taxonomic_hints": {},
        },
        "source": sources,
        "tier_counts": {"exact": 0, "same_genus": 0, "same_family": 0, "same_order": 0},
        "display_cohort": empty_cohort,
        "analysis_cohort": analysis_cohort,
        "computed_summary": {"cohort": "analysis_cohort", "record_count": 0, "metrics": {}},
        "target_position": _statistics_target_position([], target_metrics),
        "notes": [],
    }


def _unseen_rows(rows: list[dict[str, str]], seen_taxids: set[str]) -> list[dict[str, str]]:
    return [
        row for row in rows
        if str(row.get("Organism Taxonomic ID") or "").strip() not in seen_taxids
    ]


def _remember_taxids(rows: list[dict[str, str]], seen_taxids: set[str]) -> None:
    for row in rows:
        taxid = str(row.get("Organism Taxonomic ID") or "").strip()
        if taxid:
            seen_taxids.add(taxid)


def _deduplicate_by_key(
    records: list[dict[str, Any]],
    sort_key: Any,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=sort_key):
        selected.setdefault(_accession_numeric_core(record["accession"]), record)
    return sorted(selected.values(), key=sort_key)


def _assembly_level_rank(value: str | None) -> int:
    text = (value or "").casefold()
    if "complete genome" in text or "chromosome" in text:
        return 0
    if "scaffold" in text:
        return 1
    if "contig" in text:
        return 2
    return 3


def _metrics_completeness_score(record: dict[str, Any]) -> int:
    score = 0
    if record.get("scaffold_n50_bp") is not None or record.get("contig_n50_bp") is not None:
        score += 1
    if record.get("gc_percent") is not None:
        score += 1
    if record.get("busco") is not None:
        score += 1
    if record.get("chromosome_count") is not None:
        score += 1
    return score


def _statistics_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    n50 = record["scaffold_n50_bp"] if record["scaffold_n50_bp"] is not None else record["contig_n50_bp"]
    return (
        _STATISTICS_TIER_RANK[record["tier"]],
        0 if record["accession"].upper().startswith("GCF_") else 1,
        _assembly_level_rank(record.get("assembly_level")),
        -_metrics_completeness_score(record),
        -(n50 if n50 is not None else -1),
        record["accession"],
    )


def _quartile_summary(values: list[float | int]) -> dict[str, float] | None:
    if not values:
        return None
    if len(values) == 1:
        value = values[0]
        return {"min": value, "q1": value, "median": value, "q3": value, "max": value}
    all_ints = all(isinstance(value, int) for value in values)
    ordered = sorted(values)
    q1, median_value, q3 = quantiles(ordered, n=4, method="inclusive")
    result = {"min": min(values), "q1": q1, "median": median_value, "q3": q3, "max": max(values)}
    if all_ints:
        return {key: value if key in ("min", "max") else round(value, 2) for key, value in result.items()}
    return {key: round(float(value), 2) for key, value in result.items()}


def _statistics_computed_summary(analysis_records: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for field in ("total_sequence_length_bp", "contig_n50_bp", "scaffold_n50_bp", "gc_percent"):
        values = [record[field] for record in analysis_records if record.get(field) is not None]
        summary = _quartile_summary(values)
        if summary is not None:
            metrics[field] = summary
    busco_complete = [
        record["busco"]["complete"]
        for record in analysis_records
        if record.get("busco") is not None and record["busco"].get("complete") is not None
    ]
    summary = _quartile_summary(busco_complete)
    if summary is not None:
        metrics["busco_complete_percent"] = summary
    busco_duplicated = [
        record["busco"]["duplicated"]
        for record in analysis_records
        if record.get("busco") is not None and record["busco"].get("duplicated") is not None
    ]
    summary = _quartile_summary(busco_duplicated)
    if summary is not None:
        metrics["busco_duplicated_percent"] = summary

    chromosome_values = [record["chromosome_count"] for record in analysis_records if record.get("chromosome_count") is not None]
    chromosome_count_mode = None
    if chromosome_values:
        counts = Counter(chromosome_values)
        chromosome_count_mode = min(value for value, count in counts.items() if count == max(counts.values()))

    busco_lineages = sorted({
        record["busco"]["lineage"]
        for record in analysis_records
        if record.get("busco") and record["busco"].get("lineage")
    })

    by_tier = {"exact": 0, "same_genus": 0, "same_family": 0, "same_order": 0}
    by_accession_type = {"GCF": 0, "GCA": 0}
    by_assembly_level = {"chromosome": 0, "scaffold": 0, "contig": 0, "unknown": 0}
    level_bucket_by_rank = {0: "chromosome", 1: "scaffold", 2: "contig", 3: "unknown"}
    for record in analysis_records:
        by_tier[record["tier"]] += 1
        if record["accession"].upper().startswith("GCF_"):
            by_accession_type["GCF"] += 1
        elif record["accession"].upper().startswith("GCA_"):
            by_accession_type["GCA"] += 1
        by_assembly_level[level_bucket_by_rank[_assembly_level_rank(record.get("assembly_level"))]] += 1

    return {
        "cohort": "analysis_cohort",
        "record_count": len(analysis_records),
        "metrics": metrics,
        "chromosome_count_mode": chromosome_count_mode,
        "busco_lineages": busco_lineages,
        "cohort_composition": {
            "by_tier": by_tier,
            "by_accession_type": by_accession_type,
            "by_assembly_level": by_assembly_level,
        },
    }


def _percentile_rank(values: list[float], target: float) -> float | None:
    if not values:
        return None
    less = sum(1 for value in values if value < target)
    equal = sum(1 for value in values if value == target)
    return round((less + 0.5 * equal) / len(values) * 100, 1)


def _statistics_target_position(
    analysis_records: list[dict[str, Any]],
    target_metrics: dict[str, float] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "n50_percentile": None,
        "assembly_size_percentile": None,
        "gc_percentile": None,
        "busco_complete_percentile": None,
        "busco_duplicated_percentile": None,
        "basis": dict(_STATISTICS_BASIS),
        "notes": [],
    }
    if target_metrics is None:
        result["notes"].append("target_metrics_not_provided")
        return result

    any_available = False

    def _resolve(field_key: str, percentile_key: str, target_key: str, values: list[float]) -> None:
        nonlocal any_available
        target_value = target_metrics.get(target_key)
        if target_value is None:
            result["notes"].append(f"{percentile_key}_unavailable:no_target_{target_key}")
            return
        if not values:
            result["notes"].append(f"{percentile_key}_unavailable:no_cohort_{field_key}_data")
            return
        result[percentile_key] = _percentile_rank(values, target_value)
        any_available = True

    # Use best available N50 (scaffold preferred, contig fallback) for fair comparison
    n50_values = []
    for record in analysis_records:
        n50 = record["scaffold_n50_bp"] if record.get("scaffold_n50_bp") is not None else record.get("contig_n50_bp")
        if n50 is not None:
            n50_values.append(n50)
    _resolve("n50_bp", "n50_percentile", "n50_bp", n50_values)

    size_values = [record["total_sequence_length_bp"] for record in analysis_records if record.get("total_sequence_length_bp") is not None]
    _resolve("total_sequence_length_bp", "assembly_size_percentile", "assembly_size_bp", size_values)

    gc_values = [record["gc_percent"] for record in analysis_records if record.get("gc_percent") is not None]
    _resolve("gc_percent", "gc_percentile", "gc_percent", gc_values)

    busco_complete_values = [
        record["busco"]["complete"]
        for record in analysis_records
        if record.get("busco") is not None and record["busco"].get("complete") is not None
    ]
    _resolve("busco_complete_percent", "busco_complete_percentile", "busco_complete_percent", busco_complete_values)

    busco_duplicated_values = [
        record["busco"]["duplicated"]
        for record in analysis_records
        if record.get("busco") is not None and record["busco"].get("duplicated") is not None
    ]
    _resolve("busco_duplicated_percent", "busco_duplicated_percentile", "busco_duplicated_percent", busco_duplicated_values)

    result["available"] = any_available
    return result


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
