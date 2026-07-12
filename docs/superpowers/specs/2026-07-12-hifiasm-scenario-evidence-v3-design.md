# Hifiasm Scenario EvidenceCase v3 Design

**Date:** 2026-07-12  
**Status:** Approved for implementation  
**Scope:** Assembly parameter-recommender knowledge ingestion and initial evidence retrieval for `hifiasm` and `hifiasm_ont`

## 1. Purpose

Replace the current source-heavy hifiasm EvidenceCase v2 contract with a small, repository-native EvidenceCase v3 model that:

- stores one curator-authored `methods.md` beside each paper case;
- treats structured YAML spans as the model-consumed evidence;
- removes `body.md`, source-document SHA256, exact quote matching, quote offsets, and document-coverage analysis;
- models assembly input topology and phasing mode explicitly;
- retrieves evidence by scenario compatibility before optional numeric similarity;
- supports both HiFi hifiasm and ONT-simplex hifiasm-ONT cases;
- preserves the existing validated-case, ranked-case, prompt-budget, and citation attestation boundaries;
- seeds four approved paper cases supplied and reviewed by the project owner.

This is a small contract refactor, not another compatibility layer. The obsolete v2 source model is deleted rather than supported in parallel.

## 2. Problems In The Current Design

### 2.1 Source ingestion is over-specified

The existing contract requires a `body.md`, a `methods.md`, two source hashes, exact and uniquely located quotes, offset tracking, and document coverage calculations. These controls were designed as though the knowledge pack were ingesting arbitrary full papers. In practice, the repository stores short curator-authored evidence dossiers. Git revision history plus schema/provenance validation is the appropriate integrity boundary.

### 2.2 Scenario compatibility is under-specified

The existing applicability model primarily uses one `read_type` string. It cannot reliably distinguish:

- HiFi plus Hi-C phasing;
- HiFi plus ultra-long ONT bridging;
- ONT R10.4.1 simplex mode;
- Trio HiFi with paternal and maternal short-read k-mers.

The prompt profile already exposes `has_hifi`, `has_ont`, `has_ultra_long`, `has_hic`, and `has_trio`, but retrieval does not use the complete topology.

### 2.3 ONT evidence is outside the current v2 numeric path

The current numeric retriever is hard-coded around HiFi compatibility. A paper case for `hifiasm_ont` would either be excluded or fall back to a legacy path, defeating the purpose of the new curated evidence model.

### 2.4 Production cases are currently empty

The production hifiasm v2 shard contains no cases. Therefore, this is the correct point to replace the unpublished production authoring contract rather than preserve a permanent v2/v3 compatibility burden.

## 3. Design Principles

1. **Scenario first, numbers second.** Input topology and assembly mode are hard compatibility facts. Genome size and read statistics are optional ranking features.
2. **One human evidence dossier.** Each paper directory contains one `methods.md`; no synthetic `body.md` is required.
3. **Structured YAML is authoritative for prompts.** YAML spans are curated evidence text, not automatically verified quotations.
4. **No fake precision.** Missing numeric facts remain absent; they are never replaced with zero or inferred defaults.
5. **Separate route facts from recommendable parameters.** Input flags and workflow modes remain L0/L1 route context, not L2 parameter recommendations.
6. **Delete obsolete machinery.** No v2 source checksum or quote-matching compatibility path remains.
7. **Keep runtime safety boundaries.** Validated-case attestation, ranked selection attestation, prompt budgets, and response citation validation remain.
8. **Avoid unrelated refactors.** Retry state, approval gates, artifact hashes, replay hashes, and execution lifecycle are outside this change.

## 4. Target Directory Layout

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
├── cases.yaml
├── cases/
│   ├── hifiasm/
│   │   └── papers/
│   │       ├── s41587-022-01261-x/
│   │       │   ├── case.yaml
│   │       │   └── methods.md
│   │       ├── s41592-024-02269-8/
│   │       │   ├── case.yaml
│   │       │   └── methods.md
│   │       ├── s41586-026-10105-6/
│   │       │   ├── case.yaml
│   │       │   └── methods.md
│   │       └── s41586-022-05325-5/
│   │           ├── case.yaml
│   │           └── methods.md
│   └── legacy/
│       ├── initial_hifi.yaml
│       ├── initial_ont.yaml
│       └── retry_failures.yaml
├── rules/
└── strategies.yaml
```

The manifest continues to reference shard files using the existing loader convention. Each paper `case.yaml` contains a `cases` list, normally with one scenario case. A later paper may contain more than one case only when it reports genuinely different input topologies, assembly modes, or interventions.

Legacy non-hifiasm shards may remain for their current consumers, but obsolete v2 hifiasm source fixtures and production layout are removed.

## 5. Persistent EvidenceCase v3 Contract

### 5.1 Top-level shape

```yaml
schema_version: 3
case_id: paper.s41587-022-01261-x.hifi-hic
case_type: initial
paper_id: s41587-022-01261-x

source:
  kind: paper
  title: null
  doi: 10.1038/s41587-022-01261-x
  year: 2022

applicability: ...
observed_facts: ...
intervention: ...
rationale_claims: ...
outcomes: ...
evidence: ...
curation: ...
```

`paper_id` groups cases derived from the same paper. `case_id` identifies the scenario retrieval unit.

### 5.2 Evidence block

```yaml
evidence:
  methods_path: methods.md
  spans:
    - span_id: applicability
      text: >
        Diploid sample with PacBio HiFi and Hi-C, without parental
        sequencing, for haplotype-resolved assembly.
    - span_id: command.hifi_hic
      text: |
        hifiasm \
          -o <outputPrefix> \
          --h1 <HiC-reads-R1.fasta> \
          --h2 <HiC-reads-R2.fasta> \
          -t <nThreads> \
          <HiFi-reads.fasta>
```

Authoring rules:

- `methods_path` is required for approved cases.
- The path must be relative, confined to the knowledge-pack root, and resolve to a regular file.
- The file basename is `methods.md`.
- A span contains only `span_id` and `text`.
- Span IDs are non-empty and unique within the case.
- Span text is non-empty and bounded.
- The validator does not require span text to occur in `methods.md`.
- The validator does not read the document for quote matching, offsets, hashes, or coverage.
- Only spans referenced by structured fields are projected downstream.

The runtime Task 2 projection retains the existing downstream interface:

```yaml
source_excerpts:
  - span_id: command.hifi_hic
    document: methods
    quote: "..."
```

`document: methods` and `quote` are compatibility fields synthesized by Task 2. They are not part of the persistent v3 YAML contract.

### 5.3 Curation

```yaml
curation:
  status: approved
  curator: project_owner
  reviewed_at: "2026-07-12"
  notes: >
    Curated from the project owner's paper and supplementary-method summary.
```

Only approved cases are retrieval eligible. Approved cases require a curator and a valid `methods.md` path. Draft and rejected cases may be incomplete but remain ineligible.

## 6. Scenario Applicability Model

### 6.1 Primary reads

`primary_reads` describes the reads assembled into the primary graph:

```yaml
primary_reads:
  technology: pacbio_hifi
  subtype: null
```

or:

```yaml
primary_reads:
  technology: ont
  subtype: r10_4_1_simplex
```

Controlled technologies initially include:

- `pacbio_hifi`
- `ont`

Controlled ONT subtypes initially include:

- `r10_4_1_simplex`
- `ultra_long`

Ultra-long ONT used only as a bridging auxiliary input is not the primary read type.

### 6.2 Auxiliary inputs

```yaml
auxiliary_inputs:
  hic: required
  ultra_long_ont: absent
  parental_short_reads: absent
```

Allowed states:

- `required`
- `optional`
- `absent`
- `unknown`

These are scenario conditions, not numeric features. `unknown` means the curated evidence does not establish the condition; it must not be interpreted as absent.

For Trio cases, parental input can be represented more specifically:

```yaml
parental_short_reads:
  state: required
  relationships: [paternal, maternal]
  role: parental_kmer_binning
```

The implementation may use a uniform nested record for all auxiliary inputs if that keeps validation clearer, provided the four-state semantics remain explicit.

### 6.3 Assembly goal

```yaml
assembly_goal:
  product: haplotype_resolved
  phasing_strategy: hic
  target_level: chromosome
```

Controlled values initially include:

- `product`: `primary`, `primary_and_alternate`, `haplotype_resolved`, `partially_phased`
- `phasing_strategy`: `none`, `hic`, `trio`, `read_based`
- `target_level`: `contig`, `chromosome`, `near_t2t`, `reference_grade`

Product and phasing strategy are strong compatibility fields. Target level is a weaker compatibility field unless a case-specific method requires it.

### 6.4 Target method

```yaml
method:
  id: hifiasm
```

or:

```yaml
method:
  id: hifiasm_ont
```

The validator and selector support both methods under the shared v3 scenario contract.

## 7. Observed Facts And Numeric Ranking

Observed numeric facts remain optional and evidence-cited:

- genome size in Mb;
- primary-read coverage;
- primary-read N50 in kb;
- heterozygosity;
- ploidy.

Rules:

- A present numeric fact must be finite and within the existing biological sanity bounds.
- A present numeric fact must cite one or more v3 evidence spans.
- Missing facts are omitted.
- Auxiliary-read coverage and N50 are not mapped into primary-read numeric fields.
- Outcomes never participate in input similarity ranking.
- Input technology, Hi-C, UL-ONT, Trio, phasing mode, tool version, and command flags are categorical scenario facts, not numeric distance features.

Retrieval classifies numeric support as:

- `numeric_comparable`: enough shared features exist to compute a meaningful distance;
- `scenario_only`: compatibility is exact but numeric overlap is insufficient;
- `numeric_distant`: compatible scenario with a large distance, usable only as weak evidence.

No case is made artificially comparable by inserting zero values.

## 8. Intervention Model

```yaml
intervention:
  tool:
    id: hifiasm
    version: 0.16.1-r375

  route:
    mode: hic_phasing
    evidence_span_ids: [command.hifi_hic]

  reported_parameters:
    hom_cov:
      cli_flag: --hom-cov
      value: null
      value_semantics: sample_specific
      reported: true
      evidence_span_ids: [command.hifi_ul_hic, parameter.hom_cov]

  commands:
    - command_id: hifi_hic
      purpose: haplotype_resolved_assembly
      evidence_span_ids: [command.hifi_hic]
```

### 8.1 Route configuration

Route configuration records workflow modes and input bindings such as:

- Hi-C R1/R2 inputs;
- UL-ONT input;
- paternal and maternal Yak databases;
- ONT simplex mode;
- dual-haplotype scaffolding mode.

These facts inform scenario compatibility and prompt context. They are not emitted as L2 parameter recommendations.

### 8.2 Reported parameters

Only behavior-changing parameters that can map to the method catalog are eligible as parameter evidence. Each reported parameter must cite a span.

Examples:

- `--hom-cov`: reported, sample-specific value;
- `--telo-m CCCTAA`: reported, explicitly species-specific;
- `--dual-scaf`: reported mode/behavior flag if cataloged as adjustable;
- `--min-overlap 10000` in Flye: downstream method evidence, not a hifiasm L2 parameter.

Execution mechanics such as `-o` and `-t` remain non-recommendable route/executor fields.

### 8.3 Commands

Full commands remain visible as curated evidence spans and human-readable methods sections. They are not flattened into invented parameter facts. The case extracts only route facts and catalog-mappable reported parameters.

## 9. Rationale Claims

```yaml
rationale_claims:
  - claim_id: combine_hifi_hic
    statement: >
      HiFi provides a high-accuracy assembly graph and Hi-C provides
      chromosome-scale haplotype linkage without parental sequencing.
    explicitness: explicit
    effect_area: phasing
    applies_to:
      route_fields: [auxiliary_inputs.hic]
      parameters: []
    evidence_span_ids: [rationale.hifi_hic]
```

Rules:

- `explicit` means the curated source summary attributes the rationale to the paper.
- `inferred` means the curator derived the rationale from the method context.
- Explicit claims require cited spans.
- Inferred claims remain visibly labeled and may omit spans.
- Claims may associate with route fields, parameters, or both.
- A reported command with no reported rationale must not receive an invented generic rationale.

## 10. Retrieval Pipeline

The initial retrieval pipeline becomes:

```text
raw v3 paper cases
  -> Task 2 schema/provenance validation and attestation
  -> scenario compatibility filtering
  -> optional numeric ranking within compatible scenarios
  -> source diversity and top-k selection
  -> Task 3 ranked-case attestation
  -> Task 4 prompt budgeting and citation contract
```

### 10.1 Compatibility order

1. exact target method (`hifiasm` or `hifiasm_ont`);
2. primary read technology and subtype;
3. required/absent auxiliary input compatibility;
4. phasing strategy;
5. assembly product;
6. target level preference;
7. optional numeric ranking.

A topology mismatch is never repaired by numeric similarity.

### 10.2 Query construction

The retrieval query uses existing prompt-profile facts:

- `assets.read_type`;
- `assets.has_hifi`;
- `assets.has_ont`;
- `assets.has_ultra_long`;
- `assets.has_hic`;
- `assets.has_trio`;
- sample ploidy;
- target level;
- genome/read numeric facts.

It must not read the currently absent `sample.assembly_goal` path and silently default every query to `primary`. Product/phasing intent should be derived from route/method selection and available input topology, or represented as unknown when not determinable.

### 10.3 Ranking

Within the compatible scenario set:

1. exact scenario tier;
2. meaningful numeric-overlap eligibility;
3. numeric distance ascending;
4. evidence coverage descending;
5. clade as a weak tie-break;
6. stable case ID ordering.

This replaces the current coverage-first behavior that can reward a distant but fully reported case over a close but partially reported case.

### 10.4 Paper diversity

`paper_id` is included in retrieval metadata. By default, only one case per paper is selected unless another scenario from the same paper has a distinct, directly relevant intervention. This prevents one paper with several branches from being counted as several independent studies.

## 11. Four Initial Approved Cases

### 11.1 `s41587-022-01261-x`

- Method: `hifiasm`
- Version: `0.16.1-r375`
- Primary reads: PacBio HiFi
- Required auxiliary input: Hi-C
- Parental reads: absent
- Goal: haplotype-resolved assembly using Hi-C phasing
- Commands:
  - HiFi + Hi-C phased assembly
  - HiFi-only partially phased fallback
- Key rationale: accurate HiFi graph plus chromosome-scale Hi-C haplotype linkage

### 11.2 `s41592-024-02269-8`

- Method: `hifiasm`
- Version: `0.19.5-r587`
- Primary reads: PacBio HiFi
- Required auxiliary input: ultra-long ONT
- Hi-C: optional for the described scalable near-T2T route
- Goal: diploid or polyploid near-T2T assembly
- Commands:
  - UL read filtering at 100 kb for HPRC Year 2
  - UL read filtering at 50 kb for HPRC Year 1 and plant samples
  - HiFi + UL-ONT + Hi-C hifiasm command
- Key parameter: `--hom-cov` is sample-specific and derived from the homozygous coverage peak
- Key rationale: UL-ONT bridges repeats while HiFi provides accuracy; filtering avoids assembly errors and reduces runtime

### 11.3 `s41586-026-10105-6`

- Method: `hifiasm_ont`
- Version: `0.25.0-r726`
- Primary reads: ONT R10.4.1 standard simplex
- Dedicated UL preparation: absent
- Goal: partially phased near-T2T assembly
- Command flags:
  - `--ont`
  - `--dual-scaf`
  - `--telo-m CCCTAA`
- Key rationale: read phasing separates stable heterozygous variants from recurrent sequencing errors
- Transfer restriction: the human telomere motif must not be copied to another species without species-specific evidence

### 11.4 `s41586-022-05325-5`

- Method: `hifiasm`
- Final cited trio version: `0.14.1`; explicitly reported test command used v0.3
- Primary reads: PacBio HiFi
- Required auxiliary inputs: paternal and maternal short reads for Yak k-mers
- Hi-C/Omni-C: available in the reference-grade workflow
- UL-ONT: downstream haplotype-binned gap filling, not a hifiasm primary input in the cited Trio command
- Goal: trio-phased, reference-grade diploid assembly
- Commands:
  - paternal Yak count
  - maternal Yak count
  - Trio hifiasm command
  - haplotype-binned Flye command
- Key rationale: graph-based trio phasing outperformed non-graph phasing; ONT-UL fills GA-rich and other complex gaps
- Provenance warning: the final hifiasm v0.14.1 production command is not fully reported and must not be reconstructed as though explicit

## 12. Removed V2 Design

The following are deleted from the EvidenceCase source contract and their tests/fixtures/docs:

- `body.md`;
- `source_documents.body`;
- source-document `sha256` fields;
- SHA256 format and checksum comparison;
- raw span `document` discriminator;
- exact quote/file substring validation;
- quote unique-offset validation;
- overlapping quote ambiguity detection;
- quote start/end metadata;
- complete-document quote rejection;
- merged interval calculations;
- 90% document coverage checks;
- per-document excerpt coverage budgets;
- methods-document type checks that become tautological in a methods-only contract.

The old v2 production shard/layout and obsolete fixture body files are deleted. Old design documents are marked superseded rather than silently rewritten as historical records.

## 13. Preserved Safety And Runtime Invariants

The refactor must retain:

- confined relative paths and symlink escape rejection;
- closed persistent and runtime schemas;
- approved/draft/rejected eligibility semantics;
- required curator for approved cases;
- unique span IDs;
- valid evidence-span references;
- evidence requirements for numeric facts, reported parameters, and explicit rationales;
- per-field, per-span, per-case, and Task 4 prompt budgets;
- projection of referenced spans only;
- omission audit when prompt budgets drop evidence;
- Task 2 validation-owned semantic attestation;
- Task 3 ranked-selection attestation;
- Task 4 canonical prompt attestation;
- catalog parameter allowlists and L0/L1/L2 boundaries;
- response case/span citation validation;
- runtime artifact, prompt, decision, command, replay, ledger, and evaluation hashes.

## 14. Error Handling

Validation errors remain deterministic configuration errors and identify:

- case ID;
- field path;
- manifest or shard path;
- missing/escaping methods path;
- duplicate or unknown span ID;
- invalid controlled vocabulary;
- incompatible target method/read topology;
- missing approved curator;
- missing required evidence citations;
- prompt budget violation.

An invalid approved case is never silently skipped.

## 15. Testing Strategy

### 15.1 Source contract

Test that:

- approved methods-only case succeeds;
- body, source SHA256, and raw span document fields are rejected as obsolete;
- missing, absolute, traversal, non-file, and symlink-escaping methods paths fail;
- YAML span text need not occur in methods.md;
- duplicate/empty/oversized spans fail;
- total span budget remains enforced;
- only referenced spans are projected;
- runtime projection synthesizes `document: methods` and `quote`.

### 15.2 Provenance and attestation

Test that:

- numeric facts require spans;
- reported parameters require spans;
- explicit rationale requires spans;
- inferred rationale remains labeled;
- ordinary dictionaries cannot bypass Task 2;
- mutation invalidates attestation;
- draft/rejected cannot self-promote;
- Task 3 and Task 4 accept only their appropriate attested inputs.

### 15.3 Scenario compatibility

Add focused cases for:

- HiFi + Hi-C;
- HiFi + UL-ONT;
- ONT simplex;
- Trio HiFi + parental reads.

Verify topology mismatches are excluded before numeric ranking. Verify `hifiasm_ont` uses the v3 path rather than legacy evidence selection.

### 15.4 Numeric ranking

Verify:

- distance precedes evidence completeness after compatibility;
- insufficient overlap produces `scenario_only` rather than fabricated distance;
- missing numeric facts are not treated as zero;
- top-k ordering is deterministic;
- paper-level diversity is deterministic.

### 15.5 Prompt and parameter evidence

Retain and run existing tests for:

- span and serialized-byte budgets;
- whole-span omission without truncation;
- omission audit;
- visible case/span citations;
- unrelated span rejection;
- same-effect-area evidence rules;
- full methods file/path not entering the prompt;
- L0/L1 parameters remaining non-recommendable.

### 15.6 Regression

Run focused Phase 11 suites plus assembly regressions covering:

- case selection;
- prompt payload building;
- Methods-aware response normalization;
- parameter evidence validation;
- offline evaluation reproducibility;
- parameter approval and retry boundaries;
- artifact hashes and replay binding.

## 16. Implementation Boundaries And Dirty Worktree Safety

The worktree already contains unrelated uncommitted Phase 11 work. Implementation must:

- edit narrow hunks rather than replace files wholesale;
- not restore, reset, or overwrite user changes;
- preserve current runtime `source_excerpts` shape;
- avoid unnecessary edits to `methods_evidence.py` and retry/lifecycle modules;
- migrate shared test helpers before editing individual assertions;
- delete only EvidenceCase source SHA logic, never global artifact SHA logic;
- inspect diffs after every implementation slice.

## 17. Acceptance Criteria

The refactor is complete when:

1. no production EvidenceCase code requires body documents, source SHA256, exact quote matching, quote offsets, or document coverage;
2. the persistent v3 contract uses one methods path and structured YAML spans;
3. all four paper cases are approved, valid, and loadable;
4. each paper has a human-readable `methods.md` containing the supplied scenario, rationale, commands, caveats, and version information;
5. `hifiasm` and `hifiasm_ont` both use v3 scenario-aware initial evidence retrieval;
6. scenario topology is evaluated before numeric similarity;
7. missing numeric facts remain missing and can yield `scenario_only` evidence;
8. runtime attestation, prompt budgets, citation validation, catalog safety, and artifact hashes remain intact;
9. obsolete v2 source fixtures and code are removed rather than hidden behind compatibility branches;
10. focused and regression tests pass without reverting unrelated worktree changes.
