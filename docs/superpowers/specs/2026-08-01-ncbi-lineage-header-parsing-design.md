# NCBI Lineage Header Parsing Design

## Problem

The pinned `Metazoa.ncbi.lineage` export has two tab-separated columns, but
its second header cell is empty. The related-species loader currently uses
`csv.DictReader` and reads only a column named `Lineage`. It therefore assigns
an empty lineage set to every taxid. Same-family and same-order cohort tiers
cannot select any records even when the pinned export contains matching lineage
terms.

## Scope

Repair only lineage-table parsing in `app/reference/ncbi_metazoa.py`. Preserve
the pinned source files, manifest hashes, target resolution, tier ordering,
taxid precedence, alternate-haplotype filtering, and cohort size limits.

## Design

Keep the lineage loader's TSV `DictReader`, but resolve the lineage column from
the parsed header before reading rows.

- Require a first column headed `Organism Taxonomic ID`.
- Accept either a second column headed `Lineage` or an empty second header cell,
  which is the format of the pinned NCBI snapshot.
- Ignore rows with an empty taxid, matching current behavior.
- Preserve an empty lineage value as an empty term set, matching current
  behavior.
- Split each non-empty lineage value on semicolons and normalize each term.
- Preserve the current last-row-wins behavior for duplicate taxids.

The resulting `dict[str, set[str]]` remains the loader's public internal
contract, so `_rows_with_lineage_term` and all downstream cohort logic remain
unchanged.

## Error Handling

Unreadable files continue to raise `OSError` for the caller's existing
non-blocking unavailable-pack handling. A header without `Organism Taxonomic
ID`, or without either a `Lineage` or empty lineage-column name, raises
`ValueError`; the existing caller converts that into an explainable unavailable
pack. Row handling otherwise remains unchanged, and the parser will not guess
a taxid or lineage column by content.

## Tests

Keep the existing standard-header cross-tier test unchanged. Add regression
tests that write the production-compatible blank-second-header layout and
assert:

- the statistics pack collects same-genus, same-family, and same-order records;
- the evidence pack can fall back to same-family or same-order records; and
- a malformed lineage header produces the existing explainable unavailable
  result instead of silently returning empty tiers.

Run the targeted evidence/statistics tests and directly verify the pinned
snapshot now has non-zero same-family and same-order tier counts for
`Solea ovata`.
