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

Replace the lineage loader's header-dependent `DictReader` use with a
two-column TSV reader.

- Require a first column headed `Organism Taxonomic ID`.
- Accept either a second column headed `Lineage` or an empty second header cell,
  which is the format of the pinned NCBI snapshot.
- Reject rows without both a non-empty taxid and a lineage value.
- Split each lineage value on semicolons, normalize each term, and union terms
  for duplicate taxids rather than overwriting earlier values.

The resulting `dict[str, set[str]]` remains the loader's public internal
contract, so `_rows_with_lineage_term` and all downstream cohort logic remain
unchanged.

## Error Handling

Unreadable files continue to raise `OSError` for the caller's existing
non-blocking unavailable-pack handling. A malformed header or non-two-column
row raises `ValueError`, which the existing caller also converts into an
explainable unavailable pack. The parser will not guess a taxid or lineage
column by content.

## Tests

Keep the existing standard-header cross-tier test unchanged. Add a regression
test that writes the production-compatible blank-second-header layout and
asserts same-genus, same-family, and same-order records are all collected.
Run the targeted statistics tests and directly verify the pinned snapshot now
has non-zero same-family and same-order tier counts for `Solea ovata`.
