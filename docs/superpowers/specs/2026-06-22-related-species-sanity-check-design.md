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
canonical scientific-name column, and optional aliases or notes. The resolver
accepts either an alias/Chinese name or an already scientific name.

The evidence builder searches `Metazoa.ncbi.tsv` in this order:

1. Exact canonical scientific-name match.
2. If exact matching returns no assembly, same-genus assemblies, based on the
   first taxonomic token of the canonical scientific name.

For each selected public assembly, retain accession, assembly method, total
sequence length, chromosome count, contig and scaffold counts, contig and
scaffold N50, GC percentage, and BUSCO lineage when available. The builder
returns an explicit matching mode (`exact`, `same_genus`, or `unavailable`) and
never asserts that a related species is the target species.

The pack is bounded to a small, deterministic set of useful records so the LLM
receives evidence rather than the entire NCBI table.

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

## Failure and degradation semantics

This node is non-blocking. Missing species metadata, an unresolved alias,
missing/unreadable local reference data, or no matching assemblies produces a
valid evidence pack with an explanatory status and empty evidence list. The
rest of assembly review, including any existing human gate, continues
unchanged.

The LLM prompt must state that related-species values are comparative evidence
only. It should identify plausible or implausible differences, describe what
they might mean, and recommend follow-up checks; it must not treat such values
as truth or advise altering a previously executed assembly.

## Tests

- Chinese name resolves through the alias table; scientific name bypasses alias
  lookup.
- Exact NCBI assembly matches take precedence over same-genus records.
- A target with no exact assembly returns selected same-genus evidence.
- Missing target name, alias, source file, or matching assemblies yields a
  non-blocking explanatory pack.
- The assembly-review graph includes the new node without adding a top-level
  stage or route.
- The JSON and Markdown reports contain the evidence artifact and section.
- LLM recommendation context includes both the project's result metrics and
  the related-species evidence, with the non-substitution constraint.
