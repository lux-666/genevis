# Task 03: Unify Sparse Parameter Recommendation for Assembly, Hi-C, and T2T

**Phase**: 13
**Status**: Planned
**Depends on**: Tasks 01-02

## Goal

Define one parameter-recommendation contract for Assembly, Hi-C, and T2T:

> The LLM returns only the parameters that should be explicitly set for the
> current attempt. `parameters` is a sparse mapping and may be empty. A key that
> is not present is not added to the recommendation, not reviewed separately,
> and not copied from another attempt.

Assembly already implements most of this behavior for its parameter mapping.
Phase 13 must preserve that behavior, remove the remaining model-controlled
retry routing, and use the same sparse contract when Hi-C and T2T are expanded.

## Canonical Semantics

### One mapping per attempt

Each recommendation attempt owns exactly one mapping:

```json
{
  "parameters": {
    "parameter_name": "explicit_value"
  },
  "summary": "Optional reviewer-facing explanation"
}
```

The mapping contains only explicit L2 choices for that attempt. There is no
separate `omitted_parameters` field and no stored catalog-difference list.

`parameters: {}` is valid. It means that the model recommends no explicit L2
overrides for this attempt.

### Retry creates a fresh mapping

A retry produces a new attempt-local sparse mapping from its own model
response. It does not begin with the prior mapping and has no merge, patch, or
inheritance step.

Assembly retry example:

```text
attempt 0 recommendation: {"purge_level": 2, "kmer_length": 51}
attempt 1 recommendation: {"purge_level": 1}

attempt 1 L2 input:        {"purge_level": 1}
```

Attempt 1 has exactly one L2 parameter: `purge_level`. `kmer_length` belongs
only to attempt 0 and has no state in attempt 1.

The previous attempt remains immutable retry evidence. It is never a parameter
baseline and never a source used to complete the new mapping.

### Retry prompt history

For retry attempt `N`, the prompt receives a bounded chronological projection
of attempts `0..N-1`. For each earlier attempt, include the available immutable
evidence needed to understand what was tried and what happened:

- the exact sparse recommendation and human approval decision;
- the frozen effective parameter/config provenance when execution was built;
- execution status and bounded result evidence, including relevant metrics and
  log excerpts;
- collect, failure, and human-review outcomes when present.

An earlier attempt that was rejected or failed before execution remains in the
history with that outcome; the prompt must not make it look as though it ran.
Large records stay artifact-backed, while the prompt uses a deterministic,
bounded projection whose rendered messages are persisted exactly.

Code owns evidence collection, ordering, integrity checks, and prompt
projection only. It does not derive a parameter delta, select which keys must
change, require a previous value to be retained or removed, or turn an earlier
result into an automatic retry decision. The model may repeat a value, change
it, return different keys, or return `{}`. Any shape-valid result still goes to
the human approval gate as the new attempt's exact sparse mapping.

### Defaults remain outside the recommendation

The recommendation artifact and approval payload contain only the sparse L2
mapping returned by the model. Gene Agent must not copy catalog defaults,
executor defaults, facts, or values from a prior attempt into that mapping.

Execution remains deterministic through separately owned sources:

```text
L0 executor mechanics
+ L1 provided/measured facts
+ implicit tool defaults or materialized wrapper defaults
+ current approved sparse L2 mapping
-> effective command/configuration
```

The recommendation source must remain distinguishable from every default. A
default is never recorded as an LLM recommendation.

For ordinary CLI options, including Assembly L2 options, an absent key is not
emitted and the tool default remains implicit. No new Assembly
`executor_defaults` group is required for values that are never materialized.

If a wrapper requires a concrete config value even when the LLM did not
recommend it, the executor may write its own declared default. That materialized
value must be provenance-labeled as executor-owned in frozen evidence and must
not be added to `recommendation.json` or the human approval payload.

If a required executable field has neither a provided fact nor an executor
default, approval may still occur, but the later simple executability check
fails explicitly. The system must not ask the model about every absent catalog
key or synthesize a value to make the command pass.

## Recommendation Boundary

### Inputs

The model receives:

- typed species facts supplied by the caller;
- bounded stage facts and the chronological recommendation/execution history
  required by a retry task;
- the complete adjustable parameter catalog as available choices;
- stage-specific guidance and selected evidence cases when configured.

Showing the complete catalog does not require the model to return every key.
The prompt must explicitly say to return only parameters that should be
explicitly set or emitted for the current attempt.

Add a strict `SpeciesFacts` model under `app/schemas/shared_parameters.py` at
`shared_parameters.species_facts`:

- required `scientific_name: str`;
- optional `taxon_id: int`;
- optional `clade: str`;
- optional `ploidy: int`;
- optional `expected_chromosome_count: int`;
- optional `telomere_motif: str`.

`RunConfig.shared_parameters.species_facts` is the authoritative producer.
Recommendation workflows must not infer species facts from filenames, free-form
manifest metadata, related-species retrieval, or an LLM. The full profile
snapshots the provided facts into immutable recommendation evidence.

### Response parsing

Response parsing checks only the declared JSON structure:

- the response is an object;
- `parameters` is a mapping, including an empty mapping;
- parameter values are direct JSON values rather than rich wrapper objects;
- optional `summary` is a non-empty string when present;
- unsupported top-level aliases are rejected.

Parsing does not require every catalog key. It does not build an absent-key
list. It does not check catalog membership, numeric ranges, biological
correctness, cross-parameter rules, or automatic-patch safety floors.

Unknown names and unusual values remain in the sparse mapping and reach human
review. Whether an approved value can be represented by the selected command or
config format is checked only at the later executability boundary.

### Mandatory approval

Every shape-valid recommendation enters a hash-bound human approval gate,
including:

- `parameters: {}`;
- mappings containing one explicit parameter;
- mappings containing unknown or unusual values;
- initial and retry recommendations.

The model does not approve, reject, stop, or divert its own recommendation.
Remove model-controlled `retry_disposition` from the shared Assembly contract
and do not introduce it for Hi-C or T2T. Retry-stop and expert-escalation choices
belong to human review or operator-owned retry policy.

The approval gate shows the exact current mapping and optional summary. It may
state that all absent keys will use their ordinary execution/tool behavior, but
it must not generate one approval question per absent key.

## Durable Records

Persist these records for every attempt:

- full input profile;
- immutable catalog/basis snapshot;
- exact rendered model messages and prompt identity;
- raw provider response;
- parsed sparse recommendation or parse failure;
- response-parse result;
- recommendation manifest;
- hash-bound human approval decision.

`recommendation.json` and the approval request contain the exact sparse mapping.
They do not contain an application-completed mapping or an absent-key list.

After approval, `ParameterAttempt` owns the recommendation and the eventual
frozen/execution refs. Frozen command/config evidence separately records:

- the approved sparse L2 mapping;
- materialized executor-owned wrapper defaults actually used, when any;
- L0/L1 values;
- the resulting effective parameters and argv/config.

## Stage-Specific Surface

### Assembly

Keep the current behavior where `parameters` may contain any subset of L2 and
`build_parameter_sources()` passes only that mapping as `l2_recommended`.

Assembly's existing L0 catalog defaults remain L0 executor mechanics. This task
does not reclassify them or add an executor-default source group for implicit
CLI defaults.

Phase 13 Assembly cleanup is limited to the contract inconsistency:

- remove required `retry_disposition` from retry responses;
- remove `stop` / `expert_review` model routing before the approval gate;
- hydrate every Assembly retry prompt with the bounded chronological evidence
  for all earlier parameter attempts and their execution/review outcomes;
- keep retry-history code evidence-only: it must not compute or enforce the
  next mapping;
- remove retry-only cursor/status variants that no longer have a human or
  operator owner;
- route every shape-valid retry mapping to `parameter_approval`;
- update prompt guidance, response parsing, workflow routing, architecture docs,
  and tests.

### Hi-C

Keep runtime mechanics model-hidden:

- threads and Python command;
- tool/model/Juicer/3D-DNA paths;
- input/output paths;
- retry budget and review policy.

The AutoHiC adjustable catalog may expose:

- `restriction_enzyme`;
- `number_of_edit_rounds`;
- `translocation_adjust`;
- `inversion_adjust`;
- `debris_adjust`;
- `error_max_epoch`;
- `error_min_len`;
- `error_max_len`;
- `error_filter_score`;
- `error_filter_iou_score`.

The LLM may return any subset, including none. A provided restriction enzyme is
an L1 library fact. If the sparse mapping does not contain
`restriction_enzyme`, command construction uses the provided fact; if neither
exists and AutoHiC requires it, the post-approval executability check blocks.

Other absent AutoHiC values use documented executor/tool defaults. Those
defaults remain outside the recommendation and approval records.

### T2T

Keep these values model-hidden:

- operator `mode`;
- input assembly and ONT read refs;
- chromosome-list ref;
- minimap2 executable, preset, threads, paths, and output locations;
- attempt identity and review policy.

The species telomere motif is an L1 provided fact. The adjustable sparse surface
is:

- `min_motif_copies`;
- `terminal_window_bp`;
- `min_mapq`;
- `max_target_terminal_distance_bp`;
- `min_overlap_bp`;
- `min_supporting_reads`.

T2T recommendation runs after input/species-fact resolution and before scans
that consume the threshold profile. Any subset of the six thresholds may be
recommended. The executor overlays the approved sparse mapping on its declared
T2T defaults to create the effective threshold profile, while preserving the
source split in frozen evidence.

Those defaults are ordinary stage behavior, not recommendation validation. Do
not add a second minimum, clamp, hard boundary, or automatic-patch safety floor
around the approved sparse mapping.

Replace the T2T minimap2 lifecycle's direct attempt owner with
`ParameterAttemptOwner`. Parameter approval occurs before assembly/read scans;
the approved attempt then owns the minimap2 frozen and execution refs. Task 02's
collect adapter remains unchanged because it loads executions through the
owner-neutral lifecycle API.

T2T Phase 13 supports initial recommendation only. T2T retry is out of scope.

## Simple Executability Check

After approval, the existing freeze/config/command boundary may check only
whether execution can be constructed:

- required inputs and environment resources exist;
- approved values can be represented by the declared CLI/config type;
- required config fields can be resolved from L0, L1, executor defaults, or the
  current approved sparse mapping;
- a complete frozen attempt can be persisted.

It may not apply biological rules, catalog ranges, hidden safety floors, or an
automatic parameter repair. Failure persists a bounded freeze error and leaves
the approved recommendation unchanged.

## Implementation Surface

- `app/llm/parameters/assembly/`
- `app/stages/assembly/parameters/workflow.py`
- `app/stages/assembly/routing.py`
- `app/review/gates.py`
- `app/schemas/artifacts.py`
- `descriptors/knowledge_packs/assembly/parameter_recommender/SKILL.md`
- `app/schemas/shared_parameters.py`
- `app/schemas/run_config.py`
- `app/llm/parameters/hic/`
- `app/llm/parameters/t2t/`
- `app/stages/hic_scaffolding/parameters/`
- `app/stages/t2t/parameters/`
- `app/stages/t2t/builder.py`
- `app/stages/t2t/routing.py`
- `app/stages/t2t/nodes.py`
- Hi-C and T2T command/config adapters, catalogs, prompts, and descriptors

## Tests

- `parameters: {}` is valid and enters human approval.
- A one-key mapping remains one key in recommendation and approval artifacts.
- Response parsing does not require every catalog key or persist an absent-key
  list.
- Attempt 1 is not merged with attempt 0.
- A later retry prompt contains the ordered recommendations and execution
  outcomes of all earlier attempts, including attempts that did not execute.
- Retry-history projection does not enforce a parameter delta or constrain
  which sparse keys the model may return.
- Executor defaults never appear as LLM-recommended values.
- An explicitly returned L2 value remains recommended and is emitted even when
  it happens to equal the documented tool default.
- CLI flags are emitted only for keys in the current approved sparse L2 mapping.
- Required wrapper config defaults remain executor-owned and are provenance
  labeled separately.
- Unknown and unusual shape-valid values reach human review.
- Assembly retry no longer requires or routes on `retry_disposition`.
- Every shape-valid Assembly, Hi-C, and T2T recommendation enters approval.
- The T2T `parameter_approval` gate is registered and dispatches its resume
  decision to the T2T parameter workflow.
- Rejected and structurally invalid responses never freeze or run.

## Acceptance Criteria

- [ ] Sparse `parameters` mappings, including `{}`, are accepted.
- [ ] No recommendation or attempt stores an absent-key/catalog-difference list.
- [ ] Retry mappings are attempt-local and never merge with a predecessor.
- [ ] Retry prompts contain bounded, ordered recommendation and execution
      evidence for all earlier attempts.
- [ ] Retry-history code supplies evidence without making parameter decisions.
- [ ] Defaults and facts remain separate from LLM-recommended values.
- [ ] Absent CLI parameters are not emitted.
- [ ] Required wrapper defaults are recorded as executor-owned when used.
- [ ] Assembly retry cannot bypass human approval through model output.
- [ ] Hi-C and T2T use the same sparse contract with stage-specific DTOs.
- [ ] Only the post-approval executability boundary may reject an approved
      mapping as unrenderable.
