# Stage Parameter Recommenders

## Definition

A parameter recommender runs after method selection. It asks an LLM for the
parameters to present to a human reviewer. It does not select the method, route
the parent graph, approve its own answer, or synthesize a fallback answer.

Assembly and Hi-C share lifecycle and persistence concepts, but they do not
share one universal biological DTO.

## Module ownership

```text
app/llm/parameters/
├── assembly/
│   ├── request.py       # strict model-input DTO
│   ├── prompt.py        # DTO -> exact provider messages
│   ├── recommender.py   # model call + JSON parsing
│   └── response.py      # one declared response contract
└── hic/
    ├── request.py       # AutoHiC-specific model-input DTO
    └── recommender.py   # prompt rendering, model call, response parsing

app/stages/assembly/parameters/
├── profile.py           # audit profile and task-scoped facts
├── basis.py             # immutable catalog/evidence snapshot
├── catalog.py           # executor/catalog metadata
├── workflow.py          # persistence, approval, retry transitions
└── storage.py           # Assembly artifact adapter

app/stages/hic_scaffolding/parameters/
├── catalog.py
├── workflow.py
└── storage.py
```

The `llm/parameters/<stage>` packages own everything at the model boundary.
The stage packages own workflow state, durable evidence, approval, retry, and
execution hand-off. Catalogs remain stage-side because they also define command
construction and are not merely prompt assets.

## Required flow

```text
selected method + run facts
  -> full audit profile
  -> immutable basis snapshot
  -> strict stage-specific request DTO
  -> exact rendered messages persisted
  -> same messages object sent to the model
  -> raw response persisted
  -> response-shape parser
  -> exact parameters persisted
  -> mandatory human approval
  -> approved parameters handed to freeze/command construction
```

There is no recommendation patch merge, agent default, catalog-value validator,
hard-rule validator, or automatic biological approval between parsing and human
review. Unknown names and unusual values are preserved if they satisfy the
declared JSON shape. The human sees the exact parsed mapping.

Execution is a separate boundary. After approval, command construction may
still reject a parameter that cannot be represented safely by the selected
tool contract. That is an execution-definition error, not an LLM recommendation
validator and not permission to silently replace the value.

## Request DTOs

Assembly `AssemblyParameterRequestDTO` contains:

- `recommendation_contract`: `parameter_recommendation_v1`;
- `method_id` and `task`;
- `observations`;
- `adjustable_parameters` with descriptive/type metadata but no default value;
- optional `method_guidance`, `literature_selection`, and `literature_cases`;
- `retry_context` only for retry tasks.

AutoHiC `AutoHiCParameterRequest` contains:

- schema and recommendation contract;
- fixed task and method id;
- `input_facts` with library protocols and an explicitly provided enzyme;
- the single adjustable `restriction_enzyme` catalog projection.

Both DTOs forbid unknown top-level fields and exclude session identity, paths,
artifact IDs/hashes, storage metadata, credentials, and unrestricted registry
metadata. Audit artifacts may retain those facts; model DTOs may not.

## Response contracts

Initial Assembly response:

```json
{
  "parameters": {"purge_level": 1, "read_length_cutoff": 5000},
  "summary": "Optional reviewer-facing explanation."
}
```

Assembly retry responses have the same shape and additionally require
`retry_disposition` with `retry`, `stop`, or `expert_review`.

AutoHiC response:

```json
{
  "parameters": {"restriction_enzyme": "MboI"},
  "summary": "Optional reviewer-facing explanation."
}
```

These are exact contracts. Aliases such as `params`, `recommended_params`,
`parameter_patch`, direct top-level parameter keys, and rich per-parameter
wrapper objects are rejected. A parser checks only response structure and the
few fields required to continue the workflow; it does not check biological
correctness or catalog ranges.

## Durable records and checkpoints

`model_request.json` owns the exact compact-JSON messages and prompt identity.
Run, request-correlation, and retrieval-query identities are derived from the
run path, recommendation directory, and manifest rather than repeated in the
profile, basis, or model-request body.
`model_response.json` owns the raw provider response. `recommendation.json`
owns the parsed recommendation. `response_parse.json` records only whether the
declared response shape parsed and, on failure, the parser error.

The recommendation manifest binds those records by immutable refs/hashes.
Checkpoint state contains bounded cursors and refs, not the request/response
bodies. No execution attempt is added before human approval.

After approval, the compact `ParameterAttempt` is the only relation owner for
the approved parameters, constructed command, frozen runner input, execution,
and review evidence. Stage cursors select the current attempt by ID; they do not
mirror those refs in method-specific keys.

Assembly input-presence signals come from the asset registry and are snapshotted
into the typed `AssemblyInputProfile`. For example, raw or clean RNA-seq assets
set `has_rnaseq`; a synthetic `stage_outputs["rnaseq"]` entry is neither a valid
stage nor an input signal.

## Conformance rules

Tests must prove that:

- request DTOs reject accidental audit fields;
- session identity and storage locators do not reach model messages;
- persisted messages equal the messages passed to the model;
- only the declared response shape is accepted;
- unknown parameter names and values survive parsing and reach human review;
- aliases, patches, rich wrappers, and top-level compatibility fields fail;
- model failure produces `response_invalid` and never fabricates defaults;
- human approval is hash-bound to the immutable recommendation;
- approved parameters, unchanged, are the L2 input to command construction.
