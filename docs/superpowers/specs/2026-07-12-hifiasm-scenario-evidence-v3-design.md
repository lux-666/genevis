# Hifiasm Scenario EvidenceCase v3 Design

## 1. Decision

Adopt **methods-only, scenario-aware EvidenceCase v3** for hifiasm initial parameter recommendation.

This is a small replacement refactor, not a compatibility layer:

- create four approved paper cases, each in its own `case.yaml` and paired `methods.md`;
- delete the obsolete hifiasm v2 source-document contract;
- delete `body.md`, source SHA256, exact substring matching, unique offsets, and document-coverage validation;
- retrieve by input topology before numeric similarity;
- support both `hifiasm` and `hifiasm_ont` through one attested backend;
- preserve prompt budgets, parameter-level citations, Task 2/3/4 attestation, path confinement, and runtime artifact/replay hashes.

The four initial cases are one case per paper. Multi-scenario-per-paper diversity is explicitly out of scope for this refactor.

## 2. Goals And Non-goals

### 2.1 Goals

1. Make the knowledge source easy to curate and review in Git.
2. Select only cases compatible with the route actually chosen for the sample.
3. Keep paper-reported commands, rationale, limitations, and transfer constraints visible to the recommendation pipeline.
4. Prevent L0/L1 route- or profile-owned values from becoming L2 recommendations.
5. Remove old machinery instead of leaving adapters and deprecated fields behind.

### 2.2 Non-goals

- Rechecking the original PDFs.
- Pinning curator-authored Markdown with SHA256.
- Proving that YAML evidence text occurs verbatim in Markdown.
- Supporting legacy hifiasm v2 cases.
- Reworking retry-case selection.
- Renaming the existing runtime `source_excerpts` or `intervention.parameters` interfaces.
- Deleting any artifact, replay, prompt, ledger, or decision SHA/hash logic.

## 3. Target Layout

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
├── cases.yaml
└── cases/hifiasm/papers/
    ├── s41587-022-01261-x/
    │   ├── case.yaml
    │   └── methods.md
    ├── s41592-024-02269-8/
    │   ├── case.yaml
    │   └── methods.md
    ├── s41586-026-10105-6/
    │   ├── case.yaml
    │   └── methods.md
    └── s41586-022-05325-5/
        ├── case.yaml
        └── methods.md
```

Each `case.yaml` is a shard with exactly one entry:

```yaml
cases:
  - schema_version: 3
    ...
```

`cases.yaml` remains manifest version 2. Manifest version and case schema version are independent:

```yaml
version: 2
case_schema: assembly_evidence_case
case_files:
  - cases/hifiasm/papers/s41587-022-01261-x/case.yaml
  - cases/hifiasm/papers/s41592-024-02269-8/case.yaml
  - cases/hifiasm/papers/s41586-026-10105-6/case.yaml
  - cases/hifiasm/papers/s41586-022-05325-5/case.yaml
  - cases/initial_hifi.yaml
  - cases/initial_ont.yaml
  - cases/retry_failures.yaml
```

The old empty `cases/hifiasm/initial.yaml` is deleted.

`methods_path` is always **pack-root-relative**, not case-file-relative. This avoids adding loader sidecar metadata:

```yaml
methods_path: cases/hifiasm/papers/s41587-022-01261-x/methods.md
```

## 4. Canonical Persistent Schema

The following is the only accepted v3 shape. Every mapping is closed: unknown fields fail validation.

```yaml
schema_version: 3                         # required, exact integer
case_id: hifiasm.paper.s41587-022-01261-x # required, unique string
case_type: initial                       # required; only initial in v3

source:                                  # required
  kind: paper                            # required; paper only for initial corpus
  title: null                            # optional string/null
  doi: 10.1038/s41587-022-01261-x        # required string
  year: 2022                             # required integer

methods_path: cases/hifiasm/papers/.../methods.md # required string

evidence_spans:                         # required, 1..32
  - span_id: scenario.inputs             # required, unique string
    quote: "Curator-authored evidence text." # required nonblank UTF-8 text, <=2000 chars

applicability:                           # required
  method_id: hifiasm                     # hifiasm | hifiasm_ont
  primary_read:                          # required
    technology: pacbio_hifi              # pacbio_hifi | ont
    subtype: null                        # null | r10_4_1_simplex
  required_selected_inputs: [hic]        # list from controlled vocabulary
  forbidden_selected_inputs: [trio_parental_short_reads]
  optional_inputs: []
  assembly_product: haplotype_resolved   # controlled vocabulary
  phasing_strategy: hic                  # controlled vocabulary
  evidence_span_ids: [scenario.inputs]   # required, nonempty

observed_facts:                          # optional mapping; only present facts
  organism:
    value: human
    unit: null
    uncertainty: null
    evidence_span_ids: [sample.organism]
  genome_size_mb:
    value: 3100
    unit: Mb
    uncertainty:
      kind: interval
      min: 3000
      max: 3200
    evidence_span_ids: [sample.genome]

intervention:                            # required
  tool: hifiasm                          # required string; runtime interface retained
  version: 0.16.1-r375                   # optional string/null
  parameters:                            # required L2-only mapping; may be empty
    dual_scaffold:
      value: true
      reported: true
      evidence_span_ids: [command.main]
      transfer_constraints: null
  readonly_context:                      # required L0/L1 mapping; may be empty
    hom_cov:
      value: null
      value_semantics: sample_specific_homozygous_coverage_peak
      evidence_span_ids: [command.hom_cov]
  commands:                              # required list; may be empty
    - command_id: hifi_hic
      tool: hifiasm
      version: 0.16.1-r375
      status: reported                   # reported | illustrative | incomplete_report
      shell: "hifiasm ..."
      evidence_span_ids: [command.main]

rationale_claims:                        # optional, 0..32
  - claim_id: hifi_hic.complementarity
    statement: "HiFi supplies an accurate graph and Hi-C supplies chromosome-scale phase links."
    explicitness: explicit               # explicit | inferred
    effect_area: haplotype_preservation  # existing effect-area vocabulary
    applies_to:
      parameters: []                     # catalog IDs
      commands: [hifi_hic]               # declared command IDs
    evidence_span_ids: [rationale.hifi_hic]

limitations:                             # optional, 0..32
  - limitation_id: telomere.human_only
    statement: "CCCTAA is a human telomere motif and must not transfer to other taxa."
    applies_to:
      parameters: [telomere_motif]
      commands: [ont_simplex]
    evidence_span_ids: [limitation.telomere]

outcomes:                                # optional mapping; same fact shape as observed_facts
  contig_n50_mb:
    value: 100
    unit: Mb
    uncertainty: null
    evidence_span_ids: [outcome.n50]

curation:                                # required
  status: approved                       # draft | approved | rejected
  curator: BGI                           # required nonblank for approved
  reviewed_at: 2026-07-12                # optional string/null
  notes: null                            # optional string/null
```

### 4.1 Controlled vocabularies

`required_selected_inputs`, `forbidden_selected_inputs`, and `optional_inputs` use:

- `hic`
- `ultra_long_ont`
- `trio_parental_short_reads`
- `hifi`

The three lists must be disjoint.

`assembly_product` uses:

- `haplotype_resolved`
- `near_t2t`
- `partially_phased_near_t2t`
- `reference_grade_diploid`

`phasing_strategy` uses:

- `hic`
- `trio`
- `ont_read_phasing`
- `partial_graph_phasing`

### 4.2 Required and optional facts

`observed_facts` and `outcomes` are sparse. Missing means unknown; the validator never creates zero, false, or an empty string.

Allowed fact IDs remain:

- observed: `organism`, `clade`, `genome_size_mb`, `coverage`, `read_n50_kb`, `heterozygosity`, `ploidy`;
- outcomes: `assembly_size_mb`, `contig_n50_mb`, `busco_complete`, `busco_duplicate`.

Every present fact requires at least one valid `evidence_span_id` in an approved case.

The fact object is closed and has exactly `value`, `unit`, `uncertainty`, and
`evidence_span_ids`. `value` is a JSON scalar; numeric fact IDs require finite numbers.
`unit` is string/null. `uncertainty` is null or the closed mapping
`{kind, min, max}`. The existing vocabulary is unchanged: `kind` is `point` or
`interval`; `range` is invalid. `min` and `max` are finite numbers and are required
for `interval`; a point uncertainty is exactly `{kind: point, min: null, max: null}`.
Evidence span lists contain 1..32 unique IDs for approved cases.

### 4.3 L0/L1 versus L2

`intervention.parameters` may contain only catalog parameters whose level is 2. Keys are catalog IDs, never CLI spellings.

Route-owned or sample-owned values go into `readonly_context`, including:

- `hom_cov` (`--hom-cov`, L1);
- input files and route bindings such as `--h1`, `--h2`, `--ul`, `-1`, and `-2` (L0);
- `--ont` route mode (L0).

They may be shown as reported context but can never become adjustable parameter evidence.

The new nested objects are closed as follows:

- `intervention.parameters` is a mapping of at most 64 unique L2 catalog IDs to
  `{value, reported, evidence_span_ids, transfer_constraints}`. `reported` is a
  boolean. Approved reported entries require 1..32 unique span IDs.
- `intervention.readonly_context` is a mapping of at most 64 unique L0/L1 catalog
  IDs to `{value, value_semantics, evidence_span_ids}`. `value` may be null;
  `value_semantics` is a required nonblank string of at most 1,000 characters;
  approved entries require 1..32 unique span IDs. Route bindings use existing IDs
  including `hic_r1`, `hic_r2`, `ul_reads`, `hap1_yak`, `hap2_yak`, and `ont_mode`.
- `intervention.commands` is a list of at most 32 commands with unique
  `command_id`. Every command contains exactly
  `{command_id, tool, version, status, shell, evidence_span_ids}`. `command_id` and
  `tool` are required nonblank strings; `version` is string/null; `status` is
  `reported`, `illustrative`, or `incomplete_report`; `shell` is a nonblank string
  for `reported`/`illustrative` and string/null for `incomplete_report`; approved
  commands require 1..32 unique span IDs.
- `rationale_claims` contains at most 32 unique `claim_id` objects and uses exactly
  `{claim_id, statement, explicitness, effect_area, applies_to,
  evidence_span_ids}`. `applies_to` contains exactly `parameters` and `commands`,
  each a unique-ID list. The existing eight-value `HIFIASM_EFFECT_AREAS`
  vocabulary is unchanged.
- `limitations` contains at most 32 unique `limitation_id` objects and uses exactly
  `{limitation_id, statement, applies_to, evidence_span_ids}`. `applies_to`
  contains exactly `parameters` and `commands`, each a unique-ID list. Approved
  limitations require 1..32 unique span IDs.
- `source.title`, `intervention.version`, `curation.reviewed_at`, and
  `curation.notes` are the only nullable text fields. All other declared text is
  nonblank and at most 1,000 characters unless an explicit field limit is stated.
  `evidence_spans[].quote` is the sole larger exception and remains capped at
  2,000 characters.

### 4.4 Transfer constraints

`transfer_constraints` is either null or:

```yaml
transfer_constraints:
  allowed_taxon_ids: [9606]
  on_mismatch: exclude_parameter_evidence
```

For the ONT paper, `telomere_motif=CCCTAA` has this constraint. If query taxon is absent or not allowed, the parameter fact and every command that depends on it are removed from model-visible evidence. The associated limitation remains visible in the omission audit.

`transfer_constraints` is a closed mapping. `allowed_taxon_ids` is a nonempty list
of at most 32 unique positive integers and `on_mismatch` is exactly
`exclude_parameter_evidence`.

A command depends on a constrained parameter exactly when one limitation lists
both IDs in `applies_to.parameters` and `applies_to.commands`. Transfer filtering
runs before span budgeting. On absent/mismatched taxon it:

1. removes the constrained parameter;
2. removes the dependent commands;
3. removes those IDs from model-visible rationale `applies_to` lists;
4. removes the limitation from the filtered visible case and retains its complete
   payload in an omission audit;
5. removes any now-unreferenced ordinary spans before budgeting.

The Task 4 `methods_evidence.omission_audit` is a list of at most 32 closed
mappings:

```yaml
- case_id: hifiasm.paper.s41586-026-10105-6
  reason: transfer_constraint_taxon_mismatch
  parameter_ids: [telomere_motif]
  command_ids: [ont_simplex]
  retained_limitations:
    - limitation_id: telomere.human_only
      statement: "CCCTAA is a human telomere motif and must not transfer to other taxa."
      applies_to:
        parameters: [telomere_motif]
        commands: [ont_simplex]
      evidence_span_ids: [limitation.telomere]
```

The two top-level ID lists are unique and deterministically sorted.
`retained_limitations` uses the same closed limitation shape as the case and is
ordered by `limitation_id`. Its `applies_to` IDs intentionally identify objects
removed by transfer filtering and therefore are not dangling visible references.
The omission audit is included in canonicalization, byte budgeting, Task 4
attestation, and mutation/forgery checks. Retry inherits the exact attempt-0 audit.

### 4.5 Incomplete commands

A command with `status: incomplete_report` is documentary context only. It must never be synthesized into a runnable or fully reported command.

The trio case therefore records:

- the reported hifiasm v0.3 test command as `reported`;
- the final hifiasm v0.14.1 trio command as `incomplete_report` with a limitation stating that the complete final command was not reported.

### 4.6 Structural cardinality and references

Top-level required fields are `schema_version`, `case_id`, `case_type`, `source`,
`methods_path`, `evidence_spans`, `applicability`, `intervention`, and `curation`.
Top-level optional fields are `observed_facts`, `rationale_claims`, `limitations`,
and `outcomes`; omission is normalized to an empty mapping/list only inside the
validated runtime object.

`source` contains exactly `kind`, `title`, `doi`, and `year`; `kind` is `paper`.
`applicability` contains exactly the eight fields shown in the canonical schema.
`intervention` contains exactly `tool`, `version`, `parameters`,
`readonly_context`, and `commands`; the three collections are required and may be
empty. `curation` contains exactly `status`, `curator`, `reviewed_at`, and `notes`;
all four keys are required, and nullable values are represented explicitly.

Case IDs, span IDs, command IDs, claim IDs, and limitation IDs are each unique in
their scope. Every parameter ID must exist in the hifiasm parameter catalog at the
required level. Every command/parameter reference in `applies_to` must resolve.
Every evidence span reference must resolve. A limitation must apply to at least one
parameter or command; rationale may use two empty lists when it explains the route
as a whole. Lists are curator ordered unless this design explicitly requires sorted
audit output.

## 5. `methods.md` Contract

`methods.md` is a curator-authored paper dossier paired with the YAML case. It contains the user-supplied scenario, rationale, versions, commands, and caveats in readable Markdown.

Validation is deliberately lightweight:

1. path is relative, contains no `..`, resolves inside the knowledge-pack root, is a regular file, and is not a symlink escape;
2. file size is 1 byte to 256 KiB;
3. file decodes as UTF-8;
4. decoded content contains non-whitespace text;
5. first nonblank line is a Markdown heading containing the DOI suffix or `case_id`.

The validator does **not**:

- compute or check SHA256;
- require a `body.md`;
- compare `evidence_spans[].quote` with Markdown;
- search substrings;
- calculate offsets, uniqueness, or document coverage;
- send the full Markdown file or its path to the model.

Repository revision is the integrity boundary for curator-authored knowledge.

## 6. Persistent-To-Runtime Projection

Task 2 validates v3 and projects it to the current attested runtime interface.

| Persistent v3 | Validated/runtime |
|---|---|
| `evidence_spans[].span_id` | `source_excerpts[].span_id` |
| no document field | `source_excerpts[].document = "methods"` |
| `evidence_spans[].quote` | `source_excerpts[].quote` |
| `intervention.parameters` | unchanged |
| `intervention.readonly_context` | retained as non-adjustable context |
| `intervention.commands` | retained |
| `limitations` | retained |
| `methods_path` | validation-only; omitted from prompt projection |
| `evidence_spans` | replaced by referenced `source_excerpts`; omitted from prompt projection |
| `curation` | attestation/audit only; omitted from prompt projection |

Only spans referenced by applicability, facts, parameters, commands, rationale, or limitations are projected.

`methods_evidence.py` continues to consume `source_excerpts`, `intervention.parameters`, and `evidence_span_ids`. It is extended only to retain limitations/commands and enforce transfer constraints. `prompt_payload.py` continues to receive an attested ranked case list. `parameter_validation.py` continues to accept case evidence only for visible L2 parameters and visible cited spans.

The runtime contracts are exact:

- **Task 2 validated case:** the persistent object with `methods_path`,
  `evidence_spans`, and `curation` removed; `source_excerpts` added as the closed
  list `{span_id, document, quote}`; `retrieval_eligible` added; and omitted
  optional collections normalized exactly to `observed_facts: {}`,
  `rationale_claims: []`, `limitations: []`, and `outcomes: {}`. No other field is
  synthesized. Task 2 attestation covers the complete projected object.
- **Task 3 ranked case:** the Task 2 prompt case plus `numeric_match`. The closed
  `numeric_match` object is `{numeric_distance, numeric_similarity_display,
  evidence_coverage, weak_evidence, feature_distances, rank_numeric}`. Existing
  per-feature distance records are unchanged. Task 3 attestation covers query,
  complete ordered trace, complete ordered cases, candidate/selected IDs, backend,
  and gate metadata.
- **Task 3 selection metadata:** retains existing fields but uses
  `retrieval_backend: hifiasm_scenario` and `hifiasm_scenario_gate` in place of
  `hifiasm_numeric_gate`. The gate is exactly
  `{initial_cutover_enabled, decision, reason}` with
  `decision: hifiasm_scenario` when active.
- **Task 4 evidence:** contains the budgeted Task 3 cases, existing evidence-budget
  fields, and the new closed `methods_evidence.omission_audit`. Commands,
  limitations, readonly context, spans, and every `applies_to` reference are
  canonicalized after transfer filtering and budgeting. Task 4 attestation covers
  the entire canonical evidence object, including omission audit and case order.

`evidence_coverage` remains the established numeric field name. The design does not
introduce a second `evidence_coverage` field.

## 7. Canonical Retrieval Query

The query is tri-state and route-aware:

```yaml
query_id: ...
method_id: hifiasm
primary_read:
  technology: pacbio_hifi
  subtype: null
selected_inputs:
  hic: true
  ultra_long_ont: false
  trio_parental_short_reads: false
  hifi: true
available_inputs:
  hic: true
  ultra_long_ont: false
  trio_parental_short_reads: unknown
  hifi: true
assembly_product: haplotype_resolved
phasing_strategy: hic
taxon_id: 9606
numeric_features: ...
categorical_context:
  clade: mammal
```

Allowed state values for every availability/selection input are `true`, `false`, or `unknown`.

### 7.1 Profile additions

The prompt profile gains explicit fields rather than inferring absence from Python truthiness:

```yaml
assets:
  primary_read_technology: pacbio_hifi | ont | unknown
  primary_read_subtype: r10_4_1_simplex | unknown
  input_availability:
    hic: true | false | unknown
    ultra_long_ont: true | false | unknown
    trio_paternal_short_reads: true | false | unknown
    trio_maternal_short_reads: true | false | unknown
    hifi: true | false | unknown
  route_inputs:
    hic: true | false | unknown
    ultra_long_ont: true | false | unknown
    trio_paternal_short_reads: true | false | unknown
    trio_maternal_short_reads: true | false | unknown
    hifi: true | false | unknown
```

`trio_parental_short_reads` is true only when both paternal and maternal route inputs are true; false if either is false; otherwise unknown.

Existing `has_*` fields may remain for other consumers, but EvidenceCase v3 does not use them because current boolean fallback collapses unknown into false.

The authoritative persisted source is an explicit nullable topology on both
`AssemblyRunConfig` and `AssemblyInputProfile`:

```yaml
assembly_topology: hifi_hic | hifi_ul_hic | trio_hifi | ont_r10_4_1_simplex | null
primary_read_subtype: r10_4_1_simplex | null
ultra_long_reads_ref: string | null
trio_paternal_short_reads_ref: string | null
trio_maternal_short_reads_ref: string | null
```

The existing `hifi_reads_ref` and `hic_reads_ref` remain. New fields are copied from
run config into the input-profile artifact without truthiness coercion. `null`
means unknown, not false.

`route_inputs` is derived only from explicit `assembly_topology` and its required
bindings; asset discovery never changes selection. The exact mapping is:

| topology | hifi | hic | ultra-long | trio parental |
|---|---:|---:|---:|---:|
| `hifi_hic` | true iff resolved, else unknown | true iff resolved, else unknown | false | false |
| `hifi_ul_hic` | true iff resolved, else unknown | true iff resolved, else unknown | true iff resolved, else unknown | false |
| `trio_hifi` | true iff resolved, else unknown | false | false | true iff both parental refs exist; otherwise unknown |
| `ont_r10_4_1_simplex` | false | false | false | false |
| null/unknown | unknown | unknown | unknown | unknown |

Run-config validation checks method/topology consistency and rejects explicitly
forbidden bindings. Required bindings are resolved after `resolve_extended_inputs`:

| topology | required resolved bindings | forbidden explicit bindings |
|---|---|---|
| `hifi_hic` | HiFi primary, Hi-C | UL, either parental side |
| `hifi_ul_hic` | HiFi primary, Hi-C, UL | either parental side |
| `trio_hifi` | HiFi primary; both parental sides are required for an exact case match | Hi-C, UL |
| `ont_r10_4_1_simplex` | ONT primary from run config or resolved registered long-read asset | HiFi, Hi-C, UL, either parental side |

An unproved primary-read technology fails query construction. Incomplete auxiliary
bindings produce `unknown` route-input states and reach retrieval, where the case
is excluded. They are blocked before command execution.
In particular, one missing trio side aggregates to unknown rather than causing a
run-config error. Explicitly conflicting Hi-C/UL/parental bindings fail topology
construction. This removes overlapping-route ambiguity without inventing inputs.

`input_availability` is separate audit context. Presence of an explicit ref or
registered compatible asset maps to true. Lack of positive evidence maps to
unknown in production; production does not synthesize false from absence.
Parental availability aggregates to true iff both sides are true, false if either
side is explicitly false, otherwise unknown. Availability never implies route
selection.

### 7.2 Deterministic route mapping

The query adapter derives intent from the selected method and route binding:

| Method/selected route inputs | Primary read | Product | Phasing |
|---|---|---|---|
| `hifiasm` + Hi-C, no trio | `pacbio_hifi` | `haplotype_resolved` | `hic` |
| `hifiasm` + UL-ONT + Hi-C | `pacbio_hifi` | `near_t2t` | `hic` |
| `hifiasm_ont` + subtype R10.4.1 simplex | `ont/r10_4_1_simplex` | `partially_phased_near_t2t` | `ont_read_phasing` |
| `hifiasm` + both parental sides | `pacbio_hifi` | `reference_grade_diploid` | `trio` |

If the state cannot prove a required subtype or parental relationship, the corresponding case is not an exact-compatible candidate.

The adapter uses `assembly_topology` as the authoritative branch and then validates
the method and refs; it does not select the first matching row. Exact mappings are:

- `hifi_hic` -> `hifiasm`, PacBio HiFi, haplotype-resolved, Hi-C;
- `hifi_ul_hic` -> `hifiasm`, PacBio HiFi, near-T2T, Hi-C;
- `trio_hifi` -> `hifiasm`, PacBio HiFi, reference-grade diploid, trio;
- `ont_r10_4_1_simplex` -> `hifiasm_ont`, ONT/R10.4.1 simplex,
  partially-phased near-T2T, ONT read phasing.

Any contradiction produces a query-construction error and no EvidenceCase shortlist;
the adapter never guesses another topology.

## 8. Compatibility And Ranking

Compatibility is evaluated before numeric distance.

### 8.1 Hard compatibility truth table

A case is compatible only when all conditions hold:

1. query `method_id` exactly equals `applicability.method_id`;
2. primary technology exactly matches;
3. if the case declares a subtype, query subtype exactly matches; `unknown` does not match;
4. every `required_selected_input` is `true` in `selected_inputs`;
5. every `forbidden_selected_input` is `false`; `unknown` does not satisfy absence;
6. query product and phasing exactly match the case values.

Availability alone never satisfies a required selected input.

Each rejected case receives stable exclusion codes:

- `method_mismatch`
- `primary_read_mismatch`
- `primary_subtype_unknown_or_mismatch`
- `required_input_not_selected:<input>`
- `forbidden_input_selected_or_unknown:<input>`
- `assembly_product_mismatch`
- `phasing_strategy_mismatch`

The retriever emits **all** applicable reasons once, in the exact order listed
above. Input-specific reasons are ordered by the controlled input vocabulary
`hic`, `ultra_long_ont`, `trio_parental_short_reads`, `hifi`. `optional_inputs`
is documentary context only and imposes no compatibility condition.

### 8.2 Numeric distance

Numeric features remain the existing five weighted features. The algorithm is deterministic:

1. use only features with finite query and case values and positive configured weight;
2. if no feature is shared, set `numeric_distance: null` and `evidence_coverage: 0`;
3. otherwise use the existing clipped normalized distance and divide by the sum of shared weights;
4. missing values are omitted, never converted to zero.

`evidence_coverage` is the sum of shared positive weights divided by the sum of all
configured positive weights.

No `close/moderate/distant` threshold is introduced in this refactor.

Compatible candidates sort by:

```text
(
  numeric_distance_is_null,       # false first
  numeric_distance_or_infinity,   # ascending
  -evidence_coverage,      # descending
  case_id                         # ascending, deterministic
)
```

With the initial one-case-per-scenario corpus, numeric distance is primarily audit metadata and a future tie-breaker.

### 8.3 Backend and attestation

Rename the backend and strategy key from `hifiasm_numeric` to `hifiasm_scenario`.

The `strategies.yaml` block is closed and retains the existing numeric settings:

```yaml
hifiasm_scenario:
  initial_cutover_enabled: true
  weak_coverage_threshold: 0.6
  distance_clip: 4.0
  epsilon: 0.000001
  scales: {genome_size_mb: ..., coverage: ..., read_n50_kb: ..., heterozygosity: ..., ploidy: ...}
  weights: {genome_size_mb: ..., coverage: ..., read_n50_kb: ..., heterozygosity: ..., ploidy: ...}
```

No legacy `hifiasm_numeric` alias is accepted after the refactor.

`case_selector.py` dispatches both short tool names:

```python
{"hifiasm", "hifiasm_ont"}
```

to the same initial EvidenceCase v3 path. Task 3 attestation accepts only `retrieval_backend == "hifiasm_scenario"` for this path and binds the exact query `method_id`. `hifiasm_ont` must never silently fall back to `structured_default` while scenario cutover is enabled.

Short-name normalization is used only for dispatch. The canonical query and case
store exact values `hifiasm` or `hifiasm_ont`; Task 3 requires equality between the
requested method, query method, and every selected case method. Task 4 additionally
binds prompt method, attested query method, backend, ordered candidate IDs, ordered
selected IDs, and projected selected cases. Independent mutation of any one of
those fields must fail for both methods.

Retry tasks remain unchanged and inherit attempt-0 evidence as before.

## 9. Prompt Evidence Rules

Task 4 preserves complete-span budgeting and citation checks.

Priority order is:

1. applicability and required-input evidence;
2. reported command and L2 parameter evidence;
3. limitations and transfer constraints required by visible commands/parameters;
4. explicit rationale;
5. observed numeric facts and outcomes;
6. other referenced evidence.

A parameter or command cannot remain visible if a required limitation span was dropped. For `telomere_motif`, the limitation is inseparable from the value.

The prompt never includes:

- `methods_path`;
- full `methods.md`;
- raw `evidence_spans`;
- curation metadata;
- source SHA256, because it no longer exists.

## 10. Four Approved Cases

### 10.1 `s41587-022-01261-x`

- method: `hifiasm`
- primary: PacBio HiFi
- required selected input: Hi-C
- forbidden: Trio parental reads, UL-ONT
- product/phasing: haplotype-resolved / Hi-C
- version: `0.16.1-r375`
- commands: HiFi+Hi-C reported command and HiFi-only partial-phasing command
- rationale: accurate HiFi graph plus chromosome-scale Hi-C phase links

### 10.2 `s41592-024-02269-8`

- method: `hifiasm`
- primary: PacBio HiFi
- required selected input: UL-ONT
- Hi-C is required because the initial stored hifiasm command uses Hi-C
- product: near-T2T
- version: `0.19.5-r587`
- commands: 100 kb and 50 kb `seqkit` filters plus HiFi+UL+Hi-C hifiasm command
- rationale: HiFi accuracy plus UL-ONT repeat-spanning length; filtering avoids assembly errors and reduces runtime
- `hom_cov` is `readonly_context`, not a recommendable parameter

Because the stored command uses Hi-C, this first case declares Hi-C required. A future HiFi+UL/no-Hi-C branch should be a separate case rather than weakening compatibility.

### 10.3 `s41586-026-10105-6`

- method: `hifiasm_ont`
- primary: ONT R10.4.1 standard simplex
- forbidden selected inputs: HiFi, dedicated UL-ONT, trio parental reads
- product/phasing: partially phased near-T2T / ONT read phasing
- version: `0.25.0-r726`
- reported L2 parameters: `dual_scaffold=true`, `telomere_motif=CCCTAA`
- route context: `--ont`
- transfer constraint: telomere motif only for taxon 9606
- rationale: read phasing separates phase-consistent heterozygous variants from recurrent errors

### 10.4 `s41586-022-05325-5`

- method: `hifiasm`
- primary: PacBio HiFi
- required selected input: both paternal and maternal short-read sides
- product/phasing: reference-grade diploid / trio
- commands: paternal/maternal `yak count`, reported hifiasm v0.3 test command, Flye v2.8.3-b1695 command
- Flye context retains `--min-overlap 10000`
- limitation: final hifiasm v0.14.1 trio command was not fully reported and must not be reconstructed
- ONT-UL/Flye/Medaka/Merfin steps are documented as downstream gap-filling context, not hifiasm L2 parameter evidence

## 11. Deleted V2 Design

Delete production code, tests, fixtures, and documentation whose sole purpose is:

- `source_documents.body`;
- `source_documents.methods.sha256`;
- any EvidenceCase source SHA256;
- exact quote/file matching;
- unique quote occurrence;
- `_start`/`_end` offsets;
- complete-document or 90% coverage analysis;
- per-document body/method provenance distinction;
- v2 schema acceptance or migration.

Do not leave deprecated aliases, dual validators, feature flags, or conversion adapters.

Keep:

- pack path confinement;
- UTF-8/nonblank methods validation;
- closed YAML schema;
- span count and character budgets;
- provenance references;
- approved/draft/rejected eligibility;
- Task 2/3/4 semantic attestation;
- prompt serialization budgets;
- all runtime artifact/replay/hash checks.

## 12. Error Handling

Approved v3 cases fail closed for:

- unknown/missing schema fields;
- invalid controlled vocabulary;
- duplicate case or span IDs;
- missing/unsafe/empty/non-UTF-8/oversized `methods.md`;
- unknown evidence span references;
- non-L2 entries in `intervention.parameters`;
- unknown catalog parameter IDs;
- invalid command/limitation references;
- missing evidence for applicability, reported parameters, reported commands, explicit rationale, limitations, or present facts;
- overlapping required/forbidden/optional input lists.

Draft/rejected cases remain non-retrieval-eligible, but must still satisfy the closed structural schema when present.

## 13. Acceptance Matrix

| Query | Must select | Must exclude |
|---|---|---|
| `hifiasm`, HiFi + selected Hi-C, no UL/trio | `s41587-022-01261-x` | ONT, Trio, UL-required |
| `hifiasm`, HiFi + selected UL + selected Hi-C | `s41592-024-02269-8` | Hi-C-only, Trio, ONT |
| `hifiasm_ont`, ONT R10.4.1 simplex, no HiFi/UL/trio | `s41586-026-10105-6` | all HiFi cases |
| `hifiasm`, HiFi + both selected parental sides | `s41586-022-05325-5` | non-Trio exact tier |

The four positive fixtures set every selected-input key explicitly:

| Fixture | topology | method | subtype | selected `(hic, ul, trio, hifi)` |
|---|---|---|---|---|
| HiFi+Hi-C | `hifi_hic` | `hifiasm` | unknown | `(true, false, false, true)` |
| HiFi+UL+Hi-C | `hifi_ul_hic` | `hifiasm` | unknown | `(true, true, false, true)` |
| ONT simplex | `ont_r10_4_1_simplex` | `hifiasm_ont` | `r10_4_1_simplex` | `(false, false, false, false)` |
| Trio | `trio_hifi` | `hifiasm` | unknown | `(false, false, true, true)` |

Every fixture also supplies all availability keys; values may be true, false, or
unknown because availability is asserted not to affect compatibility. Separate
negative fixtures cover each selected state as false and unknown.

Tests must also prove:

1. every exclusion has the expected stable reason;
2. selected order and `hifiasm_scenario` backend are deterministic;
3. `hifiasm_ont` does not fall back to `structured_default`;
4. unknown subtype does not match the ONT case;
5. one missing parental side does not match the trio case;
6. available-but-not-selected auxiliary data does not satisfy a required route input;
7. missing numeric features remain missing; no zero filling;
8. no shared numeric feature yields `numeric_distance: null`;
9. full Markdown path/content is absent from the prompt;
10. telomere motif evidence is excluded for non-human or unknown taxon and its limitation is retained;
11. the trio final-command limitation remains attached to the incomplete command;
12. `hom_cov` cannot appear in L2 recommendation output;
13. Task 2/3/4 mutation and forgery rejection remains green;
14. existing artifact and replay SHA/hash tests remain green;
15. mixed manifests still load the four v3 shards and legacy non-hifiasm shards.
16. UL without selected Hi-C is excluded from the initial UL case;
17. unknown required and unknown forbidden inputs produce their stable reasons;
18. Trio with conflicting Hi-C/UL refs fails topology construction before retrieval;
19. unproved primary technology fails query construction;
20. available-but-unselected Hi-C, UL, or parental sides never satisfy a requirement.

## 14. Implementation Scope And Dirty-Worktree Safety

Before implementation, record:

```bash
mkdir -p /tmp/hifiasm-v3-baseline
git status --short --untracked-files=all > /tmp/hifiasm-v3-baseline/status.txt
git diff --binary > /tmp/hifiasm-v3-baseline/tracked.patch
git ls-files --others --exclude-standard > /tmp/hifiasm-v3-baseline/untracked.txt
git ls-files --others --exclude-standard -z | tar --null -T - -cf /tmp/hifiasm-v3-baseline/untracked.tar
```

The current deletion of `cases/hifiasm/retry.yaml` predates this refactor. Do not restore it and do not claim it as this refactor's change.

### 14.1 Production modification allowlist

- `app/knowledge/evidence_cases.py`
- `app/knowledge/packs.py` only if required for exact v3 shard validation; no manifest-version change
- `app/domains/assembly/evidence_case.py`
- `app/domains/assembly/retrieval_query.py`
- `app/domains/assembly/numeric_case_retriever.py`
- `app/domains/assembly/case_selector.py`
- `app/domains/assembly/methods_evidence.py`
- `app/domains/assembly/hifiasm_evaluation.py`
- `app/stages/assembly/parameter_profile.py`
- `app/stages/assembly/prompt_payload.py`
- `app/stages/assembly/parameter_validation.py` only if required to retain limitations/transfer filtering
- `app/schemas/run_config.py`
- `app/schemas/artifacts.py`
- `app/stages/assembly/nodes.py` only for narrow topology propagation and
  `hifiasm_numeric` -> `hifiasm_scenario` backend/gate/strategy migration hunks
- `app/entrypoints/run.py` only for narrow propagation of the five new topology/ref fields
- `descriptors/knowledge_packs/assembly/parameter_recommender/cases.yaml`
- `descriptors/knowledge_packs/assembly/parameter_recommender/strategies.yaml`
- the four new paper directories

### 14.2 Deletion allowlist

- `descriptors/knowledge_packs/assembly/parameter_recommender/cases/hifiasm/initial.yaml`
- `docs/superpowers/specs/2026-07-10-hifiasm-evidence-case-contract-design.md`
- `tests/fixtures/phase11/hifiasm_evaluation/knowledge_root/assembly/parameter_recommender/sources/eval_initial_large/body.md`
- `tests/fixtures/phase11/hifiasm_evaluation/knowledge_root/assembly/parameter_recommender/sources/eval_initial_medium/body.md`
- `tests/fixtures/phase11/hifiasm_evaluation/knowledge_root/assembly/parameter_recommender/sources/eval_initial_small/body.md`

No test source file is deleted. V2-only test cases and helpers are edited or replaced
inside the exact test-file allowlist below.

### 14.3 Test/document allowlist

- `tests/test_hifiasm_evidence_case.py`
- `tests/test_hifiasm_numeric_retrieval.py`
- `tests/test_hifiasm_methods_evidence.py`
- `tests/test_assembly_case_selector.py`
- `tests/test_phase11_prompt_payload_builder.py`
- `tests/test_parameter_evidence_validation.py`
- `tests/test_phase10_parameter_catalog.py`
- `tests/test_hifiasm_offline_evaluation.py` and its fixtures if needed
- `docs/phases/phase11/overview.md`
- `docs/phases/phase11/task_02_hifiasm_evidence_case_contract.md`
- `docs/phases/phase11/task_03_hifiasm_numeric_case_retrieval.md`
- `docs/phases/phase11/task_04_hifiasm_methods_aware_recommendation.md`
- `docs/phases/phase11/task_06_hifiasm_hybrid_retrieval_evaluation_and_cutover.md`
- `docs/user_guide/hifiasm_offline_evaluation.md`

Use narrow edits in already-dirty files. Do not restore, reset, or rewrite unrelated Phase 11 work. Compare final diffs against the recorded baseline so only newly introduced hunks are attributed to this refactor.

At completion, generate a second status/patch/untracked snapshot and compare it to
`/tmp/hifiasm-v3-baseline`. New paths and new hunks must be confined to the exact
allowlists in this section. Every pre-existing untracked file is compared against
the baseline tar archive, not treated as newly created by this refactor.

## 15. Completion Criteria

The refactor is complete when:

- the four approved `case.yaml` + `methods.md` pairs load from the production pack;
- the old empty hifiasm shard and all v2 source-verification code are gone;
- all four topology queries select the intended paper and exclude incompatible papers;
- limitations and transfer constraints survive evidence budgeting;
- only L2 catalog parameters can be recommended;
- the attested Task 2/3/4 path and artifact/replay hashes remain intact;
- targeted tests and the affected assembly regression suite pass;
- repository structure and docs describe only the v3 design, with no obsolete SHA/body/offset contract remaining.
