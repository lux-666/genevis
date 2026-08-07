# Parameter Recommendation Contract

> **Status:** implemented evidence and timeout-approval contract.
> **Read when:** exact model DTO, schema-3 persistence, sparse response,
> approval, or retry semantics matter.  
> **Comes from:** [Parameter recommendation](../topics/parameter-recommendation.md).  
> **Feeds into:** approved method-attempt freezing and review.  
> **Related contracts:** [Execution lifecycle](execution-lifecycle.md),
> [Run data](data-contract.md).  
> **Code evidence:** `app/domains/parameter_recommendation/`,
> `app/schemas/parameter_recommendation.py`,
> `app/schemas/parameter_catalog.py`.

## Definition

A parameter recommender runs after method selection. It asks an LLM for the
parameters to bind into a reviewable approval request. It does not select the
method, route the parent graph, approve its own answer, or synthesize a fallback
answer.

Assembly, Hi-C, and T2T share lifecycle and persistence concepts, but they do
not share one universal biological DTO.

## Module ownership

```text
app/llm/parameters/
├── assembly/
│   ├── request.py       # strict model-input DTO
│   ├── prompt.py        # DTO -> exact provider messages
│   ├── recommender.py   # model call + JSON parsing
│   └── response.py      # one declared response contract
├── hic/
│   ├── request.py       # AutoHiC-specific model-input DTO
│   └── recommender.py   # prompt rendering, model call, response parsing
└── t2t/
    ├── motif.py         # species-motif request, model call, response parser
    ├── request.py       # T2T threshold model-input DTO
    └── recommender.py   # prompt rendering, model call, response parsing

app/domains/parameter_recommendation/
└── facts.py             # authoritative mappings -> canonical typed facts

app/schemas/
└── parameter_recommendation.py
                         # canonical facts, typed contexts, v3 snapshots/manifests

app/stages/assembly/parameters/
├── context.py           # typed Assembly stage context
├── basis.py             # immutable catalog/evidence snapshot
├── catalog.py           # executor/catalog metadata
├── workflow.py          # persistence, approval, retry transitions
└── storage.py           # Assembly artifact adapter

app/stages/hic_scaffolding/parameters/
├── catalog.py
├── context.py
├── workflow.py
└── storage.py

app/stages/t2t/parameters/
├── catalog.py
├── context.py
├── workflow.py
└── storage.py

app/storage/
└── parameter_evidence.py
                         # strict v3 chain loader and approved bundle boundary
```

The `llm/parameters/<stage>` packages own everything at the model boundary.
The stage packages own workflow state, durable evidence, approval, retry, and
execution hand-off. Catalogs remain stage-side because they also define command
construction and are not merely prompt assets.

## Required flow

```text
selected method + allowlisted producer outputs/run facts
  -> canonical typed facts through the anti-corruption boundary
  -> schema-3 input snapshot with typed stage context and dependency refs
  -> immutable basis snapshot
  -> strict stage-specific request DTO
  -> exact rendered messages persisted
  -> same messages object sent to the model
  -> raw response persisted
  -> response-shape parser
  -> exact sparse parameters persisted
  -> immutable hash-bound parameter approval gate
  -> approved parameters handed to freeze/command construction
```

The mapping is attempt-local, may be empty, and contains only parameters that
the model recommends setting explicitly. There is no absent-key list,
recommendation patch merge, agent default, catalog-value validator, hard-rule
validator, or automatic biological approval between parsing and the immutable
approval gate. Unknown names and unusual values are preserved if they satisfy
the declared JSON shape. A claiming operator sees the exact parsed mapping.

The gate's frozen `max_blocked_seconds` controls only the default decision for
that persisted mapping. `-1` requires manual approve/reject, `0` immediately
approves it, and a positive value allows Enter to claim manual review before
expiry approves it. The timeout path submits `approve_parameters` for the
same request and recommendation hash, then delegate to the existing stage
decision applier. It may not edit the mapping, call the recommender, select a
fallback, or bypass request, occurrence, manifest, predecessor, or attempt-chain
validation.

Execution is a separate boundary. After approval, command construction may
still reject a parameter that cannot be represented safely by the selected
tool contract. That is an execution-definition error, not an LLM recommendation
validator and not permission to silently replace the value.

## Request DTOs

Assembly `AssemblyParameterRequestDTO` contains:

- `schema_version`: `3`;
- `recommendation_contract`: `parameter_recommendation_v1`;
- `method_id` and `task`;
- `facts` as strict `CanonicalRecommendationFacts`;
- typed `assembly_context` with the route, normalized read type, target level,
  and bounded chronological attempt history when retrying;
- `adjustable_parameters` with descriptive/type metadata but no default value;
- optional `method_guidance`, `literature_selection`, and `literature_cases`.

AutoHiC `AutoHiCParameterRequest` contains:

- schema-3 and recommendation contract;
- current task and fixed method id;
- canonical species facts;
- typed `autohic_context` with library protocols, an explicitly provided
  restriction enzyme, and bounded chronological attempt history when retrying;
- the adjustable AutoHiC catalog: restriction enzyme, edit rounds, correction
  switches, and error-detection thresholds.

T2T `T2TParameterRequest` contains:

- schema-3 and recommendation contract;
- current task and fixed minimap2 method id;
- canonical species facts;
- typed `t2t_context` with the resolved telomere motif, its source, and the
  exact canonical long-read member count across the platform-preserving input
  set;
- the adjustable telomere and alignment threshold catalog.

Before this threshold request, T2T performs a separate motif-resolution call
only when the canonical supplied species facts do not already contain a motif.
A supplied motif is validated and persisted as `provided_species_fact` without
a model call; otherwise the resolver requires the supplied scientific name and
persists the exact messages, raw response, parse result, and resolved source.
Pre-v3 motif/config checkpoint fields are rejected. The threshold snapshot
binds the verified motif-resolution ref, and all scan/patch consumers use only
that resolved evidence.

All DTOs forbid unknown top-level fields and exclude session identity, paths,
artifact IDs/hashes, storage metadata, credentials, and unrestricted registry
metadata. Audit artifacts may retain those facts; model DTOs may not.

## Response contracts

Every initial and retry response has the same shape:

```json
{
  "parameters": {"parameter_name": "explicit_value"},
  "summary": "Optional reviewer-facing explanation."
}
```

`parameters: {}` is valid. The model never returns an approval decision, retry
disposition, stop instruction, expert-review route, absent-key list, or mapping
patch. The hash-bound approval gate and operator-owned retry policy control
those transitions.

These are exact contracts. Aliases such as `params`, `recommended_params`,
`parameter_patch`, direct top-level parameter keys, and rich per-parameter
wrapper objects are rejected. A parser checks only response structure and the
few fields required to continue the workflow; it does not check biological
correctness or catalog ranges.

For every stage, `parameters` is the explicit L2 mapping for the current
attempt, not a patch and not a requirement to enumerate every L2 parameter. An
omitted key remains a provided fact, executor-owned default, or implicit tool
default according to the frozen command contract. Retry recommendations
independently declare the next attempt's mapping; the application does not
inherit omitted parameters from the prior attempt or require any key to change.

## Durable records and checkpoints

`input_snapshot.json` is a strict `RecommendationInputSnapshotV3` containing
`CanonicalRecommendationFacts`, one discriminated stage context, and immutable
dependency refs. `basis_snapshot.json` freezes the selected catalog and any
stage-owned knowledge basis. `recommendation_manifest.json` is the strict
`ParameterRecommendationManifestV3` root binding those records to the method,
task, recommendation ID, and predecessor identity.

`model_request.json` owns the exact compact-JSON messages and prompt identity.
Run, request-correlation, and retrieval-query identities are derived from the
run path, recommendation directory, and manifest rather than repeated in the
input snapshot, basis, or model-request body.
`model_response.json` owns the raw provider response. `recommendation.json`
owns the parsed recommendation. `response_parse.json` records only whether the
declared response shape parsed and, on failure, the parser error.

The v3 loader verifies artifact type, hash, schema, stage, method, task, and
attempt-chain identity before returning the chain. Pre-v3 roots, pre-v3 ref
names, and mixed identities fail closed. The response, parse, recommendation,
and T2T motif-manifest leaves retain their own declared schema versions; the
hard cutover applies to the recommendation snapshot/manifest root and the
approved attempt boundary. Checkpoint state contains bounded cursors and refs,
not the request/response bodies. No execution attempt is added before the
approval gate resolves.

After approval, the compact `ParameterAttempt` is the only relation owner for
the approved parameters, constructed command, frozen runner input, execution,
and review evidence. Stage cursors select the current attempt by ID; they do not
mirror those refs in method-specific keys.

Before a retry is created, the predecessor is sealed and verified. The next
model request receives a deterministic, bounded, chronological projection of
all earlier recommendations, approvals, frozen effective values, executions,
and collect/review outcomes. That history is evidence only: application code
does not score attempts, derive a parameter delta, or choose the next mapping.

Assembly input-presence signals come from the asset registry and are snapshotted
into the typed `AssemblyInputProfile`. For example, raw or clean RNA-seq assets
set `has_rnaseq`; a synthetic `stage_outputs["rnaseq"]` entry is neither a valid
stage nor an input signal. This upstream Assembly routing profile is not a
parameter-recommendation evidence record; the recommender persists the separate
schema-3 canonical input snapshot described above.

## Conformance rules

Tests must prove that:

- request DTOs reject accidental audit fields;
- session identity and storage locators do not reach model messages;
- persisted messages equal the messages passed to the model;
- only the declared response shape is accepted;
- unknown parameter names and values survive parsing and reach the approval
  gate unchanged;
- aliases, patches, rich wrappers, and undeclared top-level fields fail;
- schema-3 snapshots/manifests reject pre-v3 ref names and mixed identities;
- model failure produces `response_invalid` and never fabricates defaults;
- manual and timeout approval are hash-bound to the immutable recommendation;
- timeout approval accepts only the current persisted mapping and preserves
  the existing stage-specific validation boundary;
- timeout decisions record their request rationale plus configured and elapsed
  foreground-countdown audit facts;
- a failed automatic transaction returns to manual review only while the same
  immutable blocked request still verifies;
- approved parameters, unchanged, are the L2 input to command construction;
- empty sparse mappings still reach approval;
- retry mappings are never merged with predecessors;
- every retry prompt contains ordered evidence for all earlier attempts;
- provided facts and executor/tool defaults remain distinguishable from LLM
  recommendations in frozen evidence.
