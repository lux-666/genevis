# Task 03: Add Assembly-Style Parameter Recommendation to Hi-C and T2T

**Phase**: 13
**Status**: Planned
**Depends on**: Tasks 01-02

## Goal

Provide Hi-C and T2T with the same durable recommendation, persistence,
approval, and execution-handoff principles used by Assembly, while keeping
stage-specific biological facts and parameter surfaces.

The governing rule for Phase 13 is intentionally narrow:

> The recommender receives provided species facts. After response-shape
> parsing, no biological correctness check, catalog-range validator, hard-rule
> validator, independent automatic-patch safety floor, automatic parameter
> repair, or automatic rejection is applied. Every shape-valid recommendation
> enters a human approval gate. Only the ordinary execution/configuration
> construction path performs a simple executability check, and it never
> rewrites the approved values.

## Non-Goals

- Selecting Hi-C or T2T methods.
- Recommending execution mode or review policy.
- Recommending threads, paths, environment commands, model locations, input
  assets, output directories, attempt IDs, or other runtime mechanics.
- Automatically deciding whether a recommendation is biologically correct.
- Enforcing hidden minimums for automatic T2T patching.
- Merging a retry response as a patch over the previous recommendation.
- Executing a shape-valid recommendation without human approval.

## Shared Architecture

Hi-C and T2T share workflow concepts, not a universal model DTO:

```text
stage facts + species facts + stage catalog
  -> full audit profile
  -> immutable basis snapshot
  -> strict stage-specific request DTO
  -> exact messages persisted
  -> same messages sent to the model
  -> raw response persisted
  -> response-shape parsing only
  -> parsed recommendation persisted
  -> mandatory human approval gate
  -> compact ParameterAttempt
  -> ordinary config/command executability check
  -> freeze -> run -> collect
```

Add stage-specific model packages:

```text
app/llm/parameters/hic/
  request.py
  prompt.py
  recommender.py
  response.py

app/llm/parameters/t2t/
  request.py
  prompt.py
  recommender.py
  response.py
```

Stage workflow and storage remain under:

```text
app/stages/hic_scaffolding/parameters/
app/stages/t2t/parameters/
```

Catalogs remain stage-owned because the executor also consumes them.

## Fact Boundary

Add a strict `SpeciesFacts` model under `app/schemas/shared_parameters.py` and
place it at `shared_parameters.species_facts`. Its Phase 13 fields are:

- required `scientific_name: str`;
- optional `taxon_id: int`;
- optional `clade: str`;
- optional `ploidy: int`;
- optional `expected_chromosome_count: int`;
- optional `telomere_motif: str`.

`RunConfig.shared_parameters.species_facts` is the only authoritative producer
for this object. Recommendation workflows must not infer it from filenames,
manifest free-form metadata, related-species retrieval, or an LLM. The existing
`shared_parameters.estimated_genome_size` remains the typed source for a
provided genome-size fact.

Hi-C recommendation requires `scientific_name`. T2T recommendation requires
both `scientific_name` and `telomere_motif`; the stage must block as a missing
requirement before model invocation when either is absent. This deliberately
removes the implicit T2T `TTAGGG` fallback from the recommendation path.

Each stage snapshots the supplied object and its other bounded input facts into
its immutable full-profile artifact:

- `hic_scaffolding.parameter_input_profile`;
- `t2t.parameter_input_profile`.

The recommendation manifest owns the profile ref. Checkpoint state retains the
small typed `shared_parameters` input plus the compact recommendation cursor;
it does not duplicate the profile body.

Facts are observations, not recommendations. Missing facts remain missing; the
workflow does not invent defaults and label them as species facts. Other model
facts may include Hi-C library protocol, a provided restriction enzyme, and
bounded summaries from upstream assembly/read evidence.

Model requests must exclude session IDs, paths, artifact hashes, credentials,
storage metadata, unrestricted registry bodies, and full log/report content.
The audit profile may retain those items, but the strict request DTO must not.

## Hi-C Parameter Surface

Keep these values model-hidden and executor/operator-owned:

- `threads`;
- `python_command`;
- tool, model, Juicer, and 3D-DNA paths;
- input read paths and output paths;
- execution mode, retry budget, and review policy.

Expose provided species and library metadata as read-only facts. The initial
AutoHiC adjustable surface covers the algorithmic configuration currently
mixed into `AutoHiCRunParameters`:

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

The catalog provides names, value shapes, AutoHiC config keys, and descriptive
meaning. It does not supply an independent validator or a fallback value for
the model response. A provided restriction enzyme is visible as a fact; the
human reviewer remains responsible for confirming that the final recommended
config matches the actual library.

Split the existing run-config ownership:

- replace `AutoHiCRunParameters` with an executor-only config containing
  `threads` and `python_command`;
- add `HiCScaffoldingRunConfig.library_facts` with a strict
  `HiCLibraryFacts` object whose optional `restriction_enzyme` is an input fact,
  and keep resolved asset `library_protocol` metadata as the other fact source;
- remove all ten model-owned parameters and their defaults/range validators
  from run config and planner defaults;
- make the approved recommendation the sole source for all ten AutoHiC config
  values.

The AutoHiC freeze adapter must stop coercing, defaulting, clamping, or applying
numeric/cross-field range checks to those ten values. It may only serialize the
shape-validated approved values and reject values that cannot be represented
in the config syntax.

## T2T Parameter Surface

Keep these values model-hidden and fixed by the operator or executor:

- `mode` (`propose_only`, `manual_review`, or `auto_patch`);
- input assembly and ONT read refs;
- chromosome-list ref;
- minimap2 executable, preset, secondary-alignment policy, threads, paths, and
  output locations;
- attempt identity and review policy.

Treat the species telomere motif as a read-only provided fact, not a tunable
parameter. The initial T2T adjustable surface is:

- `min_motif_copies`;
- `terminal_window_bp`;
- `min_mapq`;
- `max_target_terminal_distance_bp`;
- `min_overlap_bp`;
- `min_supporting_reads`.

Remove `telomere_motif` and `thresholds` as executable defaults from
`T2TRunConfig`. The run config continues to own `mode` and input overrides, but
the approved recommendation is the sole source of the six thresholds and
`SpeciesFacts.telomere_motif` is the sole source of the motif. In particular,
the recommendation path must not merge `DEFAULT_THRESHOLDS` after approval.

These parameters affect evidence collection and candidate construction, so T2T
recommendation and approval must run after input/species-fact resolution and
before `scan_assembly_telomeres()`.

After approval, the T2T lifecycle should use `ParameterAttemptOwner` for the
current approved parameter attempt. The collect contract from Task 02 remains
unchanged and continues to load verified executions through the shared
lifecycle API.

## Request and Response Contracts

Each stage defines an exact request DTO containing:

- `schema_version`;
- `recommendation_contract`;
- `method_id` and recommendation task;
- typed read-only facts;
- the complete adjustable catalog projection;
- Hi-C retry context only for the retry tasks added in Task 04.

T2T Phase 13 supports `recommend_initial` only. T2T automatic retry design is
out of scope.

The response contract is intentionally small:

```json
{
  "parameters": {
    "parameter_name": "value"
  },
  "summary": "Optional reviewer-facing explanation"
}
```

Aliases such as `params`, `recommended_params`, `parameter_patch`, top-level
parameter keys, and rich per-parameter wrappers are rejected as response-shape
errors. Phase 13 Hi-C and T2T responses do not contain a model-controlled retry
disposition; a model response cannot stop, divert, approve, or reject its own
recommendation.

Shape parsing checks only:

- the response is the declared JSON object;
- `parameters` is a mapping;
- every known stage parameter is present;
- known parameter values have the primitive JSON/Python types required by the
  declared response DTO;
- the optional summary has the declared shape.

It does not check biological correctness, numeric ranges, cross-parameter
policy, similarity to defaults, or automatic-patch safety floors. Unknown or
unusual values that satisfy the declared response shape remain visible to the
human reviewer. Extra parameter names are preserved as part of the parsed
mapping and also reach human review; they are not silently discarded.

All ten known Hi-C parameters and all six known T2T parameters are structurally
required. Completeness is part of the response shape, not a biological or range
check. A retry returns another complete Hi-C mapping and never inherits values
from its predecessor. There is no recommendation patch merge and no
post-recommendation default fill.

## Persistence and Approval

For every recommendation attempt, persist:

- full input profile;
- immutable catalog/basis snapshot;
- exact rendered messages and prompt identity;
- raw provider response;
- parsed recommendation or parse failure;
- response-parse result;
- recommendation manifest binding the preceding records.

Persist the messages before invocation and pass the same in-memory message
object to the model. Checkpoint state stores only compact cursors and immutable
refs.

A shape-valid recommendation always creates a hash-bound human review request.
The approval decision must bind the recommendation manifest hash, attempt ID,
task, and predecessor identity. Allowed decisions are `approve_parameters` and
`reject_parameters`; retry orchestration may later create a new recommendation,
but approval itself never edits the proposed mapping.

## Executability Handoff

After approval, create the compact `ParameterAttempt` and hand the exact parsed
mapping to a revised stage config/command builder. The current builders must be
changed where they still own model defaults, coercion, range validation, or
cross-field policy.

The executability check is deliberately limited to whether the approved value
can be represented by the selected stage's executable/configuration contract,
for example:

- required command/config fields can be serialized;
- required input files and tool/environment resources exist;
- the resulting script/config can be frozen as a complete attempt.

An extra parameter name with no config/command representation may fail here,
after approval, as an executability-definition error. Numeric ranges,
cross-field biological plausibility, and patch safety thresholds may not fail
here when the values are still serializable.

This boundary does not add recommendation-time biological rules or safety
floors. If construction fails, persist a bounded parameter-freeze failure and
return it for human correction. Do not clamp, replace, or silently omit the
approved value.

## Implementation Surface

- `app/schemas/shared_parameters.py`
- `app/schemas/run_config.py`
- `app/schemas/artifacts.py`
- `app/entrypoints/ask_planner.py`
- `app/entrypoints/ask_interactive.py`
- `app/entrypoints/run.py`
- `app/llm/parameters/hic/`
- `app/llm/parameters/t2t/`
- `app/stages/hic_scaffolding/parameters/`
- `app/stages/t2t/parameters/`
- `app/stages/hic_scaffolding/method_application/autohic.py`
- `app/stages/t2t/nodes.py`
- Hi-C and T2T parameter catalogs, prompt bundles, and descriptors

The implementation must remove duplicate ownership rather than leave both
run-config defaults and approved model values as equal first-class paths.

## Failure Behavior

- Model invocation failure or invalid response shape persists evidence and
  stops before a human gate.
- Every shape-valid recommendation blocks for human approval.
- Rejection stops or follows an explicitly selected retry path; it never
  fabricates defaults.
- Missing/tampered recommendation or approval artifacts fail closed.
- Executability failure happens only after approval and never mutates the
  recommendation evidence.

## Tests

- Hi-C and T2T request DTOs reject audit/storage fields.
- Provided species facts reach the DTO and exact persisted messages.
- Session IDs, paths, credentials, and artifact locators do not reach the
  model.
- Persisted messages equal invoked messages.
- Only the declared response shape parses.
- Missing any known Hi-C or T2T parameter is a response-shape failure.
- Unknown or unusual shape-valid parameter values reach human approval.
- No range, biological, or automatic-patch safety validator runs between parse
  and approval.
- Every valid recommendation blocks at `parameter_approval`.
- Approval is hash-bound and exact values reach config/command construction.
- No approved mapping is merged with `DEFAULT_THRESHOLDS` or AutoHiC run-config
  defaults.
- Rejected recommendations and invalid shapes never freeze or run.
- Complete Hi-C retry mappings do not inherit values from the previous attempt.
- Hi-C and T2T use separate DTOs while sharing durable workflow helpers.

## Acceptance Criteria

- [ ] Hi-C exposes its algorithmic AutoHiC parameters through a stage catalog.
- [ ] T2T exposes its six evidence/candidate thresholds through a stage catalog.
- [ ] Species facts are typed, persisted, and projected into stage DTOs.
- [ ] `shared_parameters.species_facts` is the only species-fact source.
- [ ] T2T recommendation runs before scans that consume approved thresholds.
- [ ] Response processing performs shape parsing only.
- [ ] No hard-rule, catalog-range, biological, or automatic-patch safety check
      is introduced.
- [ ] Every shape-valid recommendation enters mandatory human approval.
- [ ] Approved values are handed unchanged to the simple executability check.
- [ ] Run-config defaults and validators no longer own model-owned Hi-C or T2T
      parameters.
- [ ] Hi-C and T2T recommendation artifacts are replayable and tamper-evident.
