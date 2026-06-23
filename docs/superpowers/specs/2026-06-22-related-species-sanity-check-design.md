# Related-species sanity check for assembly review

## Goal

Enrich the existing assembly-review result with a local, read-only NCBI
Metazoa reference-evidence pack. The evidence is used to assess whether the
project's own assembly metrics are biologically plausible for the target and
nearby species. It must never be used as an assembly input, genome-size prior,
workflow gate, or routing decision.

## Scope

- Add a `build_related_species_evidence_pack` node inside the existing
  `assembly_review` subgraph, after QUAST/BUSCO-style assessment results are
  available and before report/LLM recommendation construction.
- Resolve a user-entered Chinese or scientific species name against a manually
  maintained `data/reference/ncbi_metazoa/species_alias.tsv`.
- Query only the existing local NCBI Metazoa data files.
- Write a structured evidence-pack artifact and show its content in the
  assembly-review report and LLM context.
- Preserve the current top-level graph, assembly-review gate semantics, and
  earlier assembly parameter-selection behavior.

## Data and resolution rules

`species_alias.tsv` is a hand-maintained mapping with a Chinese-name column, a
canonical scientific-name column, and optional aliases, genus, family, order,
or notes. The resolver accepts either an alias/Chinese name or an already
scientific name. Taxonomic hints allow fallback for targets, such as species
absent from the local assembly table, whose lineage cannot otherwise be
recovered locally.

The evidence builder searches `Metazoa.ncbi.tsv` in this order:

1. Exact canonical scientific-name match.
2. If exact matching returns no assembly, same-genus assemblies, based on the
   first taxonomic token of the canonical scientific name.
3. If same-genus matching returns no useful assembly, same-family assemblies,
   using `Metazoa.ncbi.lineage` and an alias-table family hint when necessary.
4. If same-family matching returns no useful assembly, same-order assemblies,
   using `Metazoa.ncbi.lineage` and an alias-table order hint when necessary.

For each selected public assembly, retain accession, assembly method, total
sequence length, chromosome count, contig and scaffold counts, contig and
scaffold N50, GC percentage, and BUSCO lineage when available. BUSCO fields are
omitted or null when unavailable. The builder returns an explicit matching mode
(`exact`, `same_genus`, `same_family`, `same_order`, or `unavailable`) and never
asserts that a related species is the target species.

Records representing the same assembly are deduplicated before selection. A
RefSeq `GCF` record is preferred over its matching GenBank `GCA` counterpart.
The deduplication key is the accession numeric core, so `GCF_019176455.1` and
`GCA_019176455.1` are one assembly for evidence selection.
Alternate-haplotype assemblies are excluded from the primary evidence summary;
when useful for traceability, they may be retained in a separately marked
secondary list and must not count as independent primary evidence.

The primary evidence set is sorted deterministically by: primary assembly ahead
of alternate haplotype; RefSeq `GCF` ahead of GenBank `GCA`; higher scaffold
N50; higher contig N50; fewer scaffolds then contigs when the N50s tie; and
accession as the final stable tiebreaker. Keep at most five exact-match records
or, for a fallback level, at most five records from the first level with useful
assemblies. This bounds LLM context without making evidence selection
non-repeatable.

## Flow

```text
QUAST / BUSCO / existing review evidence
        ↓
build_related_species_evidence_pack
        ↓
write assembly-review Markdown and JSON reports
        ↓
existing LLM recommendation and human decision flow
```

The new node reads the manifest's user metadata for a species name, resolves
and queries the local data, writes `related_species_evidence_pack.json`, and
adds both the artifact reference and structured pack to the assembly-review
stage output. Report generation renders a `Related-species sanity check`
section. The LLM receives the pack together with the project's own assembly,
survey, and review metrics.

Each pack records its local source provenance, including the paths to
`Metazoa.ncbi.tsv`, `Metazoa.ncbi.lineage`, and `species_alias.tsv`, so a later
reader can identify the reference-data version used by the review.
Family and order fallbacks join lineage records to assembly records by taxid,
not by organism-name text. A scientific-name input is still matched against
the alias table so it may obtain its family/order hints.

The pack includes both bounded `selected_records` and a `computed_summary`
(counts plus min/median/max values for comparable assembly metrics) for direct
use in Markdown reports and LLM context.

## Failure and degradation semantics

This node is non-blocking. Missing species metadata, an unresolved alias,
missing/unreadable local reference data, or no matching assemblies produces a
valid evidence pack with an explanatory status and empty evidence list. The
rest of assembly review, including any existing human gate, continues
unchanged.

The LLM prompt must state that related-species values are comparative evidence
only. It should identify plausible or implausible differences, describe what
they might mean, and recommend follow-up checks; it must not treat such values
as truth or advise altering a previously executed assembly. It may recommend
follow-up checks or re-review, but must not reinterpret this evidence as
user-provided priors or automatic-failure thresholds.

## Tests

- Chinese name resolves through the alias table; scientific name bypasses alias
  lookup.
- Exact NCBI assembly matches take precedence over same-genus records.
- A target with no exact assembly falls back in order through same genus, same
  family, and same order, stopping at the first level with useful records.
- GCF/GCA counterparts are counted once with the GCF record selected; alternate
  haplotypes are excluded or explicitly separated from primary evidence.
- Record ordering, five-record bounds, and accession tie-breaking are stable.
- Missing target name, alias, source file, or matching assemblies yields a
  non-blocking explanatory pack.
- The assembly-review graph includes the new node without adding a top-level
  stage or route.
- The JSON and Markdown reports contain the evidence artifact, source
  provenance, and sanity-check section.
- LLM recommendation context includes both the project's result metrics and
  the related-species evidence, with the non-substitution and
  non-automatic-failure constraints.
