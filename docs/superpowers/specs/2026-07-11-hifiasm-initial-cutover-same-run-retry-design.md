# Hifiasm Initial Cutover and Same-Run Retry Design

**Date:** 2026-07-11
**Status:** Approved design boundary; implementation pending
**Phase:** 11
**Supersedes:** The Task 5/6 design that treats retry as a separately curated evidence-retrieval space

## Decision

Split Phase 11 completion into two independent concerns:

- **Task 6A — initial top-5 cutover:** enable the numeric evidence path only for `recommend_initial`, with deterministic top-5 selection and no evaluation gate.
- **Task 6B — same-run retry:** retry one sample from its own recorded attempt history. It is an ordinary run branch, not a knowledge-base feature.

The retry path must not retrieve historical successful or failed runs. It
inherits the actual immutable profile, selector/evidence, policy, and parameter
snapshot recorded for attempt 0, then reacts to the immediately preceding
execution or review failure. Execution retry and review retry remain owned by
different nodes/stages and have independent budgets.

This design deliberately deletes the retry database/retriever/backend/corpus and runtime-to-`EvidenceCase` promotion path. It does not preserve them as dormant extension points.

## Goals

1. Permit `initial_cutover_enabled` to select the deterministic numeric initial path without a corpus-size, score, coverage, evaluation, or approval gate.
2. Preserve a complete, inspectable chain for one sample across initial execution and any retries.
3. Distinguish assembly execution failures from post-assembly review failures, with separate routing, budgets, and exhaustion outcomes.
4. Reuse attempt 0's actual snapshot without re-retrieval or policy drift.
5. Keep the implementation smaller than the current retry-evidence design by removing entities that no longer serve the product behavior.

## Scope

### Task 6A: Initial-only cutover

Task 6A covers:

- all initial cases eligible under the existing loader and compatibility filters;
- initial query construction, continuous numeric retrieval, Methods-aware recommendation, catalog validation, and parameter freeze;
- deterministic selection with fixed `top_k = 5`;
- observational offline evaluation that never blocks selection, recommendation, or activation;
- production activation through the existing `initial_cutover_enabled` switch.

The complete activation contract is:

```yaml
initial_cutover_enabled: true
```

When enabled, rank every eligible initial case by the existing numeric score.
The ordering must be stable: descending numeric score, followed by one explicit
deterministic tie-break key (`case_id` ascending). Select exactly as follows:

- 5 or more eligible cases: select the first 5;
- 1-4 eligible cases: select all of them;
- 0 eligible cases: use the existing no-case behavior unchanged.

There is no minimum numeric score, evidence coverage, eligible-case count,
corpus size, query-set size, scientific approval, recorded recommendation,
threshold, non-inferiority, or default-preservation requirement for activation
or forward progress. Evaluation may report any of these measurements for
operator visibility, but its presence, absence, or result cannot change
`initial_cutover_enabled` or block the run.

### Task 6B: Same-sample retry

Task 6B covers:

- sealed lifecycle attempts for the same sample and run;
- retry after assembly execution failure;
- retry after successful assembly execution but failed assembly review;
- inheritance of attempt 0's evidence snapshot and run context;
- failure-informed parameter adjustment under the existing catalog and hard rules;
- separate execution and review retry budgets;
- a single user-visible attempt timeline and domain-specific final failure.

### Non-goals

- No `HifiasmRetryCaseRetriever` or equivalent retry retriever.
- No retry database, retry shard, retry selector backend, or `hifiasm_retry` backend.
- No 8 retry-case or 6 retry-query production gate.
- No cross-run or cross-sample historical-case retrieval.
- No creation, retention, curation, or promotion of runtime attempts as candidate `EvidenceCase` records.
- No paper re-retrieval during retry.
- No embeddings, vector database, GraphRAG, LLM Wiki, RRF, or cross-tool migration.
- No combined global retry budget. A later task may add one above the two domain budgets without merging their nodes.
- No unrelated refactor of other assemblers or their selectors.

## Architecture and Data Flow

### Task 6A initial path

```text
sample + input profile
  -> initial hifiasm query
  -> all eligible initial cases scored by existing numeric score
  -> stable sort by score desc, case_id asc
  -> fixed top_k=5 selection (or all 1-4; existing no-case path for 0)
  -> bounded Methods evidence payload
  -> initial parameter recommendation
  -> catalog/hard-rule validation and freeze
  -> immutable attempt 0 evidence snapshot
  -> assembly execution
```

After cutover, hifiasm has one declared initial selector/prompt contract. Retry is not a second selector contract; it is an attempt transition.

### Task 6B execution-failure path

```text
assembly execution fails
  -> record raw structured execution failure facts
  -> check execution retry budget
  -> build next attempt from attempt 0 snapshot + previous attempt + failure signal
  -> validate/freeze changed parameters
  -> rerun assembly
```

Execution failure input consists only of the recorded exit code/signal, bounded
stdout/stderr or log references, resource facts, rendered command, and other
existing structured execution-result fields. This design adds no failure
classifier, repairability label, or pre-retry repairability gate. If
`max_execution_retries` has budget, the execution stage creates a typed retry
request from those facts. If it is exhausted, terminate with
`execution_parameter_retries_exhausted`.

### Task 6B review-failure path

```text
assembly execution succeeds
  -> assembly review
  -> review passes: finalize success
  -> review fails:
       review-stage diagnosis
       -> check review retry budget
       -> build next attempt from attempt 0 snapshot + previous attempt + review signal
       -> validate/freeze changed parameters
       -> route back to assembly execution
```

Review signals remain directional constraints, not a universal quality score. Examples include under-purged, over-purged, fragmented, low completeness, unexpected duplication, and assembly-size ratio direction.

### Retry recommendation basis

Every retry recommendation may use only:

1. the immutable attempt 0 snapshot: `catalog_ref`, `knowledge_pack_ref`,
   hard-rule/policy ref, method-capability ref, profile ref, selected initial
   cases, available Methods spans/citations, retrieval trace, and recommendation
   rationale;
2. the immediately preceding attempt's rendered parameters and command;
3. that attempt's raw structured execution result or review result and failure signal;
4. the attempt 0 retry limits and the appropriate remaining domain budget.

Current runtime observations may only veto or block execution for safety. They
must not select, alter, rank, or justify retry parameters. A resource-related
parameter delta may be proposed only from resource facts already recorded in
the immediately preceding attempt's immutable `execution_result_ref`. Resource
observations arriving after that result may block the proposed execution, but
changing those observations cannot change the frozen delta. Catalog, knowledge
pack, hard rules, capability policy, and profile must not be reloaded mid-run.
If any configured policy hash differs from attempt 0, retry fails closed with
`policy_snapshot_mismatch`; an operator must start a new run to use the new
policy.

The failure signal justifies the delta. The inherited initial Methods evidence
explains parameter semantics and safe context; it is not falsely cited as
evidence that this exact retry succeeded elsewhere. If this basis does not
support a safe change, the branch stops or requests expert review rather than
inventing a parameter change.

## State and Artifact Contract

Reuse the existing typed parameter-attempt lifecycle and artifact references. Add fields only when an existing field cannot express the invariant.

### Run-level fields

```yaml
initial_cutover_enabled: false
initial_recommendation_contract: curated_initial_v1 # or legacy_initial
current_parameter_attempt_id: 1
retry_counts:
  execution: 1
  review: 0
retry_limits:
  execution: 1
  review: 1
parameter_attempts: [<append-only attempt refs>]
```

Configuration may continue to expose the established names `max_execution_retries` and `max_review_retries`; a duplicate `retry_limits` object is unnecessary if those names are already authoritative. The logical model above documents the two independent counters and limits, not a requirement to add another persisted entity.

### Attempt 0

Attempt 0 owns the initial evidence snapshot:

```yaml
attempt_id: 0
previous_attempt_id: null
attempt_kind: initial
initial_recommendation_contract: curated_initial_v1 # or legacy_initial
profile_ref: <immutable ref>
catalog_ref: <immutable ref>
knowledge_pack_ref: <immutable ref>
hard_rule_policy_ref: <immutable ref>
method_capability_ref: <immutable ref>
retry_limits_ref: <immutable ref>
initial_query_ref: <immutable ref>
retrieval_result_ref: <immutable ref>
evidence_payload_ref: <immutable ref>
recommendation_rationale_ref: <immutable ref>
selected_case_ids: [<0-5 stable top-ranked eligible case ids>]
frozen_parameters_ref: <immutable ref>
command_ref: <immutable ref>
execution_result_ref: <append-once ref or null>
review_result_ref: <append-once ref or null>
sealed_at: <timestamp or null>
seal_hash: <hash or null>
```

The exact storage may retain currently embedded typed values where the repository already does so. The required property is immutability and stable reference, not conversion of every value into a new artifact type.

For `legacy_initial`, curated-only evidence fields are nullable, while the
actual legacy profile, selector output, recommendation rationale, catalog, hard
rules, capabilities, and frozen parameters are still snapshotted. For
`curated_initial_v1`, the actual top-5/all/no-case retrieval result and evidence
fields are snapshotted. Retry inherits whichever actual attempt 0 contract was
recorded; it never upgrades, reinterprets, or guesses the contract.

### Retry attempt

```yaml
attempt_id: 1
previous_attempt_id: 0
attempt_kind: retry
retry_origin: assembly_execution # or assembly_review
failure_signal_ref: <immutable ref to previous attempt result/diagnosis>
previous_attempt_seal_hash: <exact seal hash carried by typed retry request>
inherited_initial_attempt_id: 0
inherited_initial_snapshot_ref: <actual legacy or curated attempt 0 snapshot>
inherited_evidence_payload_ref: <same ref as attempt 0>
inherited_profile_ref: <same ref as attempt 0>
parameter_delta:
  <parameter>:
    from: <previous frozen value>
    to: <new frozen value>
    reason: <failure-linked rationale>
frozen_parameters_ref: <new immutable ref>
command_ref: <new immutable ref>
execution_result_ref: <append-once ref or null>
review_result_ref: <append-once ref or null>
sealed_at: <timestamp or null>
seal_hash: <hash or null>
```

`retry_origin`, `failure_signal_ref`, and the parameter delta may be represented through existing typed attempt/profile fields where possible. Do not introduce a parallel retry record if the current attempt schema can carry them.

### Invariants

1. Attempt IDs are monotonic within one sample/run, and each nonzero attempt points to exactly one earlier `previous_attempt_id`.
2. Attempt IDs and `parameter_attempts` list membership are append-only. An attempt itself follows a sealing lifecycle: each planned field is written once; execution and review result fields are filled exactly once when their lifecycle event occurs; values never move backward or get overwritten.
3. Before appending attempt N+1 or constructing a terminal run result, attempt N must be complete for its reached lifecycle and sealed. Sealing writes `sealed_at` and `seal_hash` exactly once. A sealed attempt cannot be mutated.
4. Immutable referenced artifacts are content-addressed or hash-verified and are never overwritten in place.
5. Attempt 0's evidence/policy snapshot is immutable and shared by reference. Retry creates no new retrieval result, case shortlist, evidence payload, or policy reload.
6. All attempts belong to the same sample, method, run, input identity, and attempt 0 profile/policy snapshot.
7. A domain counter increments only after a retry proposal passes recommendation validation, catalog/hard-rule validation, parameter freeze, and the new attempt is successfully appended. Rejected or malformed proposals do not consume budget.
8. An execution retry increments only `retry_counts.execution`; a review retry increments only `retry_counts.review`.
9. A retry attempt records exactly one origin and one preceding failure signal.
10. Every changed parameter has a delta and failure-linked reason; unchanged parameters remain at the preceding frozen value unless a catalog dependency requires a documented coupled change.
11. Every retry parameter set passes the exact snapshotted catalog, hard-rule, capability, and freeze validation used by attempt 0.
12. Execution success is required before review. Review failure cannot be recorded for an attempt whose execution did not succeed.
13. The user-visible timeline is a deterministic derived artifact from sealed attempts; it is not a mutable source of truth.
14. A typed retry request references the sealed previous attempt by
    `previous_attempt_id` and `previous_attempt_seal_hash`; a mismatch is an
    invalid transition.

## User-visible Timeline

The final run result presents one ordered chain, regardless of which internal
stage produced each transition. `parameter_attempt_timeline` is a derived,
immutable artifact generated only from sealed attempts plus the terminal run
result:

```yaml
schema_version: 1
run_id: <run id>
sample_id: <sample id>
method_id: assembly.candidate.hifiasm
initial_recommendation_contract: curated_initial_v1 # or legacy_initial
entries:
  - sequence: 0
    attempt_id: 0
    previous_attempt_id: null
    attempt_kind: initial
    retry_origin: null
    frozen_parameters_ref: <ref>
    command_ref: <ref>
    execution:
      status: failed
      result_ref: <ref>
      failure_reason: out_of_memory
    review: null
    failure_signal_ref: <ref>
    parameter_delta: {}
    inherited_initial_snapshot_ref: <actual attempt 0 snapshot ref>
    seal_hash: <hash>
terminal:
  status: assembly_succeeded # closed enum below
  reason: completed
  final_attempt_id: 2
  execution_retries_used: 1
  review_retries_used: 1
  result_ref: <final result ref>
source_attempt_chain_hash: <hash of ordered attempt IDs + seal hashes>
```

Every entry requires sequence/identity links, attempt kind/origin, frozen
parameters, rendered command, execution outcome, review outcome when execution
succeeded and review ran, failure signal when failed, parameter delta,
inherited initial snapshot ref, and seal hash. `terminal` is required for both
success and failure. Rendering or reloading the same sealed chain must produce
the same semantic timeline and `source_attempt_chain_hash`.

The renderer reads `initial_recommendation_contract` from attempt 0 and renders
the actual attempt 0 snapshot. It must reject missing/unknown contract values or
contradictory field combinations; it must never infer the contract from case
count or the presence of evidence fields.

Successful example:

```text
attempt 0: assembly execution failed (OOM)
attempt 1: assembly succeeded; review failed (under_purged)
attempt 2: assembly succeeded; review passed
final: success
```

Exhausted example:

```text
attempt 0: assembly succeeded; review failed (fragmented)
attempt 1: assembly succeeded; review failed (low_completeness)
final: review_parameter_retries_exhausted
```

Each entry exposes the frozen parameters, rendered command, outcome, raw
execution failure facts or review diagnosis, parameter delta, and inherited
initial snapshot references. Failed attempts remain visible inside an
ultimately successful chain.

## Error Handling and Terminal States

Execution and review have domain-specific terminal states. Do not collapse them into a generic `parameter_retries_exhausted` result.

### Closed status and reason vocabulary

This design defines only these retry-routing statuses:

```text
retry_parameters_after_execution_failure
retry_parameters_after_review_failure
execution_parameter_retries_exhausted
review_parameter_retries_exhausted
parameter_retry_validation_failed
parameter_retry_invalid_transition
policy_snapshot_mismatch
assembly_succeeded
assembly_failed
```

Execution exit/log/resource facts and review measurements remain structured
payloads, not classifier or repairability enums. The retry router uses only the
generic transition reasons `execution_failed`, `review_failed`,
`execution_retry_budget_exhausted`, `review_retry_budget_exhausted`,
`retry_delta_invalid`, `missing_failure_signal`,
`contradictory_failure_signal`, `unsealed_previous_attempt`,
`previous_attempt_seal_hash_mismatch`, `policy_snapshot_hash_mismatch`, and
`completed`.

If the existing recommendation model contract already supports stop or expert
review, those outcomes remain valid with their existing status/reason schema.
This design neither adds a repairability policy nor invents a new stop/expert
status. Schema validation rejects unknown retry-routing values.

Valid terminal/transition pairings are closed as follows:

| Status | Allowed reason family | Counter effect |
|---|---|---|
| `retry_parameters_after_execution_failure` | `execution_failed` | Increment `execution` only after validated/frozen attempt append |
| `retry_parameters_after_review_failure` | `review_failed` | Increment `review` only after validated/frozen attempt append |
| `execution_parameter_retries_exhausted` | `execution_retry_budget_exhausted` | None |
| `review_parameter_retries_exhausted` | `review_retry_budget_exhausted` | None |
| `parameter_retry_validation_failed` | `retry_delta_invalid` | None |
| `parameter_retry_invalid_transition` | `missing_failure_signal`, `contradictory_failure_signal`, `unsealed_previous_attempt`, or `previous_attempt_seal_hash_mismatch` | None |
| `policy_snapshot_mismatch` | `policy_snapshot_hash_mismatch` | None |
| `assembly_succeeded` | `completed` | None |
| `assembly_failed` | Existing terminal reason outside an execution/review budget transition | None |

Tests must enumerate this pairing table, reject every invalid pairing, and
assert both counters before and after each accepted and rejected transition.

### Stage ownership and routing

| Owner | Input | May emit | Next route |
|---|---|---|---|
| Assembly execution retry node (`app/stages/assembly/*`) | Execution-failed attempt with exactly-once raw structured execution result and failure signal | seal attempt; if budget remains emit retry request referencing its seal hash, otherwise emit execution exhaustion | Assembly parameter recommendation/freeze or terminal |
| Assembly parameter recommendation/freeze | Typed retry request + attempt 0 snapshot | validated/frozen retry attempt or an existing model-contract stop/expert outcome | Append attempt, increment owning counter, then assembly execution; or existing terminal route |
| Assembly review retry node (`app/stages/assembly_review/*`) | Execution-succeeded attempt + exactly-once review result/diagnosis and failure signal | seal attempt; if budget remains emit typed review retry request referencing its seal hash, otherwise emit review exhaustion | Assembly parameter recommendation/freeze or terminal |
| Assembly execution | Typed frozen attempt only | exactly-once execution result | Review on success; execution retry node on failure |
| Assembly review | Successful execution result | exactly-once review result/diagnosis | Success terminal or review retry node |

Assembly review owns review diagnosis, review budget decisions, and construction
of review-specific terminal outcomes. Assembly receives only a typed retry
request; it does not infer review failure from loose stage state.

The ordering is identical for both domains and must not be reordered:

```text
owner writes result/diagnosis and failure signal exactly once
  -> owner seals previous attempt
  -> owner emits typed retry request with previous_attempt_seal_hash
  -> recommendation validates against that sealed snapshot
  -> catalog/hard-rule validation and freeze
  -> append attempt N+1
  -> increment only the owning domain counter
```

Any mutation or alteration of recorded failure facts/diagnosis after sealing is rejected. Recommendation
must not seal the previous attempt; it only verifies the request's seal hash.

| Condition | Required result |
|---|---|
| Execution fails and execution budget remains | Execution owner writes raw structured facts/signal, seals, and emits a seal-bound request; recommendation may choose a valid delta or an existing stop/expert outcome; append and increment only for a validated/frozen retry attempt |
| Execution budget is exhausted | `execution_parameter_retries_exhausted` with the last execution failure |
| Execution succeeds and review passes | Finalize success |
| Review fails and review budget remains | Review owner writes diagnosis/signal, seals, and emits a seal-bound typed request; proposal is validated/frozen; append an `assembly_review` retry attempt, increment review counter, and route back to assembly |
| Review budget is exhausted | `review_parameter_retries_exhausted` with the last review diagnosis |
| Failure signal is missing or contradictory | Fail closed as an invalid attempt transition; preserve artifacts |
| Retry recommendation chooses stop/expert under an existing model contract | Preserve that existing outcome; do not append an attempt or consume either budget |
| Retry parameter validation fails | Preserve the rejected recommendation artifact, do not append an attempt or increment a counter, and terminate that branch without execution |

A run may consume both budgets in sequence because the nodes are independent. For example, one execution retry followed by one review retry yields three attempts. A future total-attempt cap may wrap both routes without changing their ownership or counters.

## Deletion and Retention Matrix

| Existing concept/entity | Decision | Rationale |
|---|---|---|
| `HifiasmRetryCaseRetriever` and retry-only ranking helpers | Delete | Retry no longer retrieves cases |
| Retry query construction and retry query task contract | Delete | The next attempt is built from recorded run state, not a retrieval query |
| `hifiasm_retry` / retry `case_retrieval_backend` | Delete | There is no retry selector backend |
| Retry database/shard/configuration | Delete | No cross-run corpus exists |
| 8 retry cases + 6 retry queries gate and metrics | Delete | Not part of Task 6A or 6B acceptance |
| Initial/retry cross-contamination metrics | Delete if they exist only for the removed retry corpus | A retry retrieval space no longer exists |
| Runtime `candidate_evidence_case` artifact | Delete | Attempts are operational history, not evidence candidates |
| Candidate `EvidenceCase` curation/promotion workflow for completed runs | Delete | No runtime-to-knowledge-base path is in scope |
| Eligible initial case collection | Retain | All eligible cases participate in numeric ranking; no minimum count or scientific-approval gate is added |
| Initial numeric retriever and Methods-aware payload | Retain | They implement Task 6A |
| Initial baseline/curated evaluation | Retain as observational only | Metrics may inform operators but never gate activation or forward progress |
| Execution failure classifier, repairability label, and repairability gate | Delete | Execution retry eligibility depends only on failure plus `max_execution_retries` budget |
| Review failure diagnosis and directional metrics | Retain | They drive the review-stage retry transition |
| Typed `ParameterAttempt`, parameter delta, command, execution, and review artifacts | Retain | They are the source of truth for the same-run chain |
| Separate execution/review retry counters and limits | Retain | They preserve stage ownership and future composability |
| Catalog/hard-rule validation and freeze | Retain | It applies to every attempt |

Removal includes tests, fixtures, operator docs, descriptor fields, and prompt/schema branches whose only purpose is a deleted concept. Do not leave compatibility aliases or dead configuration keys unless an explicit persisted-data migration proves one is necessary.

## Migration and File Boundaries

Implementation should follow existing boundaries and avoid new modules unless deletion leaves a clearly reusable small unit homeless.

| Area | Expected change |
|---|---|
| `app/domains/assembly/numeric_case_retriever.py` | Keep the initial hifiasm numeric retriever; remove `HifiasmRetryCaseRetriever` and retry-only matching/projection helpers |
| `app/domains/assembly/retrieval_query.py` | Keep initial query building; remove retry retrieval-query contracts and projections |
| `app/domains/assembly/case_selector.py` | Remove retry backend registration/dispatch; declare only the initial hifiasm evidence selector where applicable |
| `app/domains/assembly/methods_evidence.py` | Keep initial Methods payload validation/budgeting; remove retry-case/candidate-promotion assumptions |
| `app/domains/assembly/hifiasm_evaluation.py` | Keep optional observational reports; remove cutover permission, minimum corpus/query requirements, pass/fail thresholds, and any ability to block activation or recommendation |
| `app/schemas/artifacts.py` | Remove candidate `EvidenceCase` and retry-retrieval-only fields/types; retain or minimally extend the typed attempt chain |
| `app/stages/assembly/nodes.py` | Keep execution retry at the assembly failure boundary; remove candidate-case emission and retry retrieval; emit execution-specific exhaustion |
| `app/stages/assembly_review/*` | Own review diagnosis, review counter/limit, typed retry-request construction, and review-specific exhaustion; route the request to assembly, which must not reconstruct review semantics |
| `app/stages/assembly/parameter_profile.py` | Build retry context from attempt 0 snapshot plus immediately preceding attempt and failure signal; do not construct retrieval queries |
| `app/stages/assembly/decision.py`, `prompt_payload.py`, `parameter_validation.py` | Express the inherited-basis retry prompt/decision and enforce delta, catalog, hard rules, and citation semantics without a retry case shortlist |
| `app/stages/assembly/routing.py`, `builder.py` | Preserve distinct execution and review nodes/routes; remove routing assumptions tied to a retry selector backend |
| Descriptor knowledge packs and fixtures | Retain eligible initial cases; remove retry case types/shards/fixtures and candidate-promotion metadata; add no minimum case count |
| Phase 11/operator docs | Rewrite Task 5/6 and cutover guidance to Task 6A/6B; remove retry corpus creation/promotion instructions |
| Tests | Delete retry-retrieval/candidate-promotion assertions; add the acceptance coverage below |

Before deletion, inventory persisted artifacts and deployed configuration for
retry backend/query/candidate-case fields and record the result in the migration
notes. If real persisted data exists, add one read-side, idempotent normalization
at the artifact-loading boundary into the sealed attempt-chain model. Re-reading
already normalized input must be a no-op. If no real persisted data exists,
record that finding and add schema/config tests that reject the old shapes and
keys. In neither case may normalization keep the old runtime behavior reachable.

## Test and Acceptance Matrix

| Area | Required tests | Acceptance |
|---|---|---|
| Initial stable top-5 | More than 5 cases, tied scores, shuffled input order, exactly 5, 1-4, and 0 eligible cases | Sort is score descending then `case_id` ascending; select first 5, all 1-4, or existing no-case behavior for 0 |
| Initial no-gate behavior | Low/negative scores, low/missing coverage, any case count, absent evaluation, failing observational metrics, no recorded recommendation set | No score/coverage/count/evaluation/approval threshold blocks selection, activation, or forward progress |
| Observational evaluation | Run and omit evaluation; vary reported retrieval/recommendation metrics | Reports remain replayable when produced and never alter `initial_cutover_enabled`, selected cases, or control flow |
| Absence of retry retrieval | Config/schema rejection of retry backend; no retry query/case fixture; no retrieval call during retry | Retry works with no retry database and does not emit a retrieval artifact |
| Attempt 0 snapshot | Stable hashes/refs before and after retries; assert no catalog/knowledge pack/hard-rule/capability/profile/evidence reload; policy hash changes mid-run | Full basis is immutable; mismatch fails closed and requires a new run |
| Runtime resource isolation | Change current resource observations after the preceding execution result; replay the same retry; exercise safety veto | Frozen delta and rationale are identical; current observations can only block execution; resource deltas use only facts recorded in the immediately preceding execution result |
| Sealing lifecycle | Exactly-once result/diagnosis/signal population, overwrite rejection, post-seal failure-fact/diagnosis mutation rejection, artifact overwrite rejection, append-before-seal rejection, terminal-before-seal rejection, request seal-hash match/mismatch, serialization/reload | Owner seals before request; request binds the exact seal hash; membership is append-only and sealed values never change |
| Execution retry routing | Raw exit/signal/log/resource fact variants, remaining/exhausted budget, accepted/rejected/stop/expert recommendation outcomes; assert no classifier or repairability gate is invoked | Any execution failure gets a retry request while budget remains; exhaustion is domain-specific; only a successfully frozen and appended retry increments the counter |
| Review retry routing | Every review reason/status, execution success + failed review, directional diagnoses, remaining/exhausted budget, accepted/rejected proposal | Review stage owns diagnosis/request/terminal; only a successfully frozen and appended review retry increments review counter |
| Mixed chain | Execution failure -> execution retry -> review failure -> review retry -> pass | Both budgets remain independent and ordered attempts produce final success |
| Parameter delta validation | Changed, unchanged, coupled, unsupported, invalid-catalog, hard-rule violation | Every change is failure-linked and validated; unsafe/unsupported deltas never execute |
| Derived timeline | Successful and failed integration chains under both `legacy_initial` and `curated_initial_v1`; required entry/terminal fields; absence of cutover decision fields; render/reload determinism; chain hash; missing/unsealed/unknown-contract/contradictory-input rejection | Timeline records the actual attempt 0 contract/snapshot without inference and includes every sealed attempt plus terminal summary |
| Candidate promotion removal | Search/config/schema and regression tests | No candidate `EvidenceCase` is emitted or promoted from runtime history |
| Legacy migration | Persisted-artifact/config inventory; idempotent read normalization when required, otherwise explicit no-data record; old schema/config rejection | Deleted retry evidence behavior cannot be re-enabled through legacy input |
| Regression | Other assemblers, existing typed lifecycle, full repository suite | No selector or retry behavior changes outside scoped hifiasm paths |

Task 6A is complete when `initial_cutover_enabled` activates the stable numeric
top-5 contract, including 1-4 and existing zero-case behavior, without an
evaluation or corpus gate.

Task 6B is complete when both same-run failure branches produce immutable, replayable attempt chains with independent budgets and domain-specific terminal outcomes, without any retry corpus, retriever, backend, or candidate evidence promotion.

## Risks and Mitigations

### Evidence drift across attempts

**Risk:** A retry silently re-runs retrieval and changes the attempt 0 basis.
**Mitigation:** Attempt 0 owns one immutable evidence snapshot; retries inherit its references and retrieval is prohibited on retry routes.

### Misrepresenting paper evidence as retry proof

**Risk:** Initial Methods text is cited as if it demonstrated that a failure-specific change succeeded.
**Mitigation:** Retry rationale separates inherited parameter semantics/safe context from the immediate failure signal that justifies the delta.

### Budget coupling through shared status

**Risk:** A generic counter or terminal status lets an execution failure consume review budget, or obscures which stage exhausted.
**Mitigation:** Separate counters, limits, routing decisions, and terminal states; test mixed chains.

### Artifact mutation during retries

**Risk:** Updating one mutable attempt object erases the failure history users need.
**Mitigation:** Append-only attempts, append-once results, stable refs/hashes, and a derived timeline.

### Dead retry-evidence architecture remains reachable

**Risk:** Dormant retriever/config/promotion paths accumulate maintenance cost or are accidentally re-enabled.
**Mitigation:** Delete the entities and their fixtures/tests/docs rather than feature-flagging them off.

### Overfitting retry policy without historical cases

**Risk:** Failure-based parameter adjustment becomes unconstrained trial and error.
**Mitigation:** Require recorded current-run failure facts, inherited
Methods/profile basis, catalog/hard rules, explicit delta, limited per-domain
budget, and any conservative stop/expert outcome already supported by the model
contract.

### Scope expansion

**Risk:** The split becomes a generalized retry framework or cross-assembler migration.
**Mitigation:** Reuse the current attempt lifecycle and stage nodes, add no new storage system, and keep Task 6A/6B hifiasm-scoped.

## Implementation Principle

Prefer deletion and reuse over abstraction. The smallest valid implementation keeps one initial evidence path, the existing typed attempt chain, and two independently owned retry transitions. Any proposed new class, store, backend, or artifact type must demonstrate that the existing attempt lifecycle cannot express the required invariant.
