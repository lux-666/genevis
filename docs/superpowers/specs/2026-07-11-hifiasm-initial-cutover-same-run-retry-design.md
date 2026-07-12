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
6. Require one explicit human approval, through the same assembly parameter gate, before freezing and executing the initial recommendation or any retry recommendation.

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

When enabled, preserve the exact lexicographic numeric ranking already approved
in Task 3: `evidence_coverage` descending, `numeric_distance` ascending, the
existing clade tie-break, then `case_id`. Task 6A does not define, collapse, or
replace that ordering with a single score; it changes only the deterministic
shortlist truncation. Select exactly as follows:

- 5 or more eligible cases: select the first 5;
- 1-4 eligible cases: select all of them;
- 0 eligible cases: use the existing no-case behavior unchanged.

There is no maximum numeric distance, minimum evidence coverage, eligible-case count,
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
- No TUI implementation or presentation change. The later TUI wrapper consumes the existing generic human-review contract.

## Architecture and Data Flow

### Task 6A initial path

```text
sample + input profile
  -> initial hifiasm query
  -> all eligible initial cases ranked by the existing Task 3 numeric contract
  -> evidence_coverage desc, numeric_distance asc, clade tie-break, case_id
  -> fixed top_k=5 selection (or all 1-4; existing no-case path for 0)
  -> bounded Methods evidence payload
  -> initial parameter recommendation
  -> automatic catalog/hard-rule validation
  -> assembly parameter approval human gate
       approve_parameters -> freeze and append immutable attempt 0 -> execute
       reject_parameters  -> terminate run without execution
```

After cutover, hifiasm has one declared initial selector/prompt contract. Retry is not a second selector contract; it is an attempt transition.

### Task 6B execution-failure path

```text
assembly execution fails
  -> record raw structured execution failure facts
  -> check execution retry budget
  -> build next attempt from attempt 0 snapshot + previous attempt + failure signal
  -> automatically validate changed parameters
  -> assembly parameter approval human gate
       approve_parameters -> freeze, append, increment execution counter, rerun
       reject_parameters  -> terminate without append, increment, or execution
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
       -> automatically validate changed parameters
       -> assembly parameter approval human gate
            approve_parameters -> freeze, append, increment review counter, execute
            reject_parameters  -> terminate without append, increment, or execution
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

The failure signal informs the delta. The inherited initial Methods evidence
explains parameter semantics and safe context; it is not falsely cited as
evidence that this exact retry succeeded elsewhere. For an execution-origin
retry request with budget remaining, recommendation must return a candidate
parameter set (which may preserve previous values) and automatic validation must
run. A valid actionable parameter candidate then blocks at the mandatory parameter
approval human gate. Human approval is the only path to freeze, append, counter increment,
and execution. Human rejection terminates the run. An actual recommendation or
parameter-validation error may terminate before the gate. Neither insufficient
evidence nor a model stop/expert choice may skip recommendation or replace the
human decision. Review-origin retries retain their existing recommendation
semantics before the same mandatory approval gate.

For an execution-origin request, a returned stop/expert action is invalid under
the recommendation schema and follows the existing recommendation-error route;
it is not a successful decision outcome.

### Shared assembly parameter approval gate

Initial and retry recommendations use one gate contract after automatic
recommendation-schema, catalog, hard-rule, capability, and parameter validation
succeeds. Reuse the existing `ReviewPayload`, `ResumeReviewRequest`,
`active_interrupt`, `blocked_human`, review-gate registry, durable decision
artifact, and resume lifecycle. Do not add a second parameter-review framework
or a TUI-specific adapter.

The gate declaration is shared by all three tasks (`recommend_initial`,
`retry_after_execution_failure`, and `retry_after_review_failure`):

```yaml
stage: assembly
gate_id: parameter_approval
decisions_allowed:
  - approve_parameters
  - reject_parameters
auto_decidable: false
```

The serialized `active_interrupt` must identify exactly one validated, not-yet-
frozen recommendation and expose at least:

```yaml
gate_id: parameter_approval
attempt_id: <proposed attempt id; 0 for initial>
task: recommend_initial # or retry_after_execution_failure / retry_after_review_failure
previous_attempt_id: null # required exact id for retry
recommendation_ref: <immutable validated recommendation artifact ref>
recommendation_hash: <content hash of that artifact and validation result>
parameter_diff: <complete diff from defaults for initial or previous frozen parameters for retry>
rationale: <bounded recommendation rationale or immutable ref>
failure_facts: <empty for initial; bounded raw execution facts or review diagnosis for retry>
evidence_refs: <attempt 0 evidence/profile/policy refs used by this recommendation>
decisions_allowed: [approve_parameters, reject_parameters]
```

These values may be carried by the generic `ReviewPayload` candidate/context,
signal, and report-ref fields and projected into `active_interrupt`; they do not
justify a parallel parameter-gate entity. Every parameter-approval
`ResumeReviewRequest` must explicitly carry `gate_id: parameter_approval`; the
applier must not accept an omitted `gate_id` through the generic request's
optional default or infer/fill it from `active_interrupt`. Its `decision` must be
exactly `approve_parameters` or `reject_parameters`. `ResumeReviewRequest.metadata`
binds the response to `attempt_id`, `task`, `previous_attempt_id`, and
`recommendation_hash`:

```yaml
stage: assembly
gate_id: parameter_approval
decision: approve_parameters # or reject_parameters
metadata:
  attempt_id: <exact proposed attempt id>
  task: <exact active task>
  previous_attempt_id: <exact active value>
  recommendation_hash: <exact active hash>
```

Validation first requires the explicit request `gate_id` to equal both
`parameter_approval` and `active_interrupt.gate_id`, and requires the decision
to be in the interrupt's closed `decisions_allowed` set. Only then may the
decision applier verify the metadata against the current `active_interrupt`,
the immutable recommendation artifact, the current attempt-chain head, and
(for retry) the sealed previous attempt. Missing or wrong `gate_id`, any unknown
decision, wrong attempt, stale/replaced recommendation, missing or mismatched
hash, changed chain head, and an already-resolved gate all fail closed. Matching
attempt metadata and hash cannot compensate for a missing/wrong gate ID or an
unknown decision. A successful decision is recorded exactly once as an immutable
human-decision artifact; duplicate approval/rejection cannot freeze, append,
increment, or execute again.

On `approve_parameters`, and only then, freeze the already validated candidate,
append its attempt, clear `active_interrupt`, leave `blocked_human`, and route to
execution. A retry increments only its owning domain counter after the frozen
attempt append succeeds. On `reject_parameters`, clear the interrupt through the
normal decision applier and terminate the run with both status and reason
`parameter_recommendation_rejected_by_human`. Rejection executes no command,
appends no retry attempt, and consumes neither execution nor review retry budget.
For an initial rejection there is likewise no frozen/executed attempt 0; the
validated recommendation and rejection decision remain terminal artifacts.

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
parameter_recommendation_ref: <immutable validated recommendation ref>
parameter_approval_decision_ref: <immutable approve_parameters decision ref>
parameter_approval_recommendation_hash: <approved recommendation hash>
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
parameter_recommendation_ref: <immutable validated recommendation ref>
parameter_approval_decision_ref: <immutable approve_parameters decision ref>
parameter_approval_recommendation_hash: <approved recommendation hash>
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
7. A proposed attempt cannot be frozen or appended before its exact validated recommendation receives `approve_parameters` at `gate_id=parameter_approval`. A domain counter increments only after approval, parameter freeze, and successful retry-attempt append. Recommendation errors, validation errors, pending approval, human rejection, stale/mismatched resume, and duplicate decisions do not consume budget.
8. An execution retry increments only `retry_counts.execution`; a review retry increments only `retry_counts.review`.
9. A retry attempt records exactly one origin and one preceding failure signal.
10. Every changed parameter has a delta and failure-linked reason; unchanged parameters remain at the preceding frozen value unless a catalog dependency requires a documented coupled change.
11. Every retry parameter set passes the exact snapshotted catalog, hard-rule, capability, and freeze validation used by attempt 0.
12. Execution success is required before review. Review failure cannot be recorded for an attempt whose execution did not succeed.
13. The user-visible timeline is a deterministic derived artifact from sealed attempts; it is not a mutable source of truth.
14. A typed retry request references the sealed previous attempt by
    `previous_attempt_id` and `previous_attempt_seal_hash`; a mismatch is an
    invalid transition.
15. Every appended attempt references exactly one immutable
    `approve_parameters` decision and the exact recommendation hash approved by
    that decision. A rejected recommendation is never represented as an attempt.
16. At most one unresolved `parameter_approval` interrupt exists per run. Its
    proposed attempt ID is the next appendable ID, and its chain head cannot
    change while the run is `blocked_human`.
17. Parameter approval resume identity is conjunctive: explicit
    `ResumeReviewRequest.gate_id == active_interrupt.gate_id ==
    "parameter_approval"`, an allowed decision, and exact metadata/hash binding
    must all pass. No field match may weaken or substitute for another.

## User-visible Timeline

The final run result presents one ordered chain, regardless of which internal
stage produced each transition. `parameter_attempt_timeline` is a derived,
immutable artifact generated only from sealed attempts and their immutable
human-decision refs plus the terminal run result:

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
    parameter_approval:
      decision: approve_parameters
      decision_ref: <immutable human-decision ref>
      recommendation_ref: <immutable validated recommendation ref>
      recommendation_hash: <approved hash>
    seal_hash: <hash>
terminal:
  status: assembly_succeeded # closed enum below
  reason: completed
  final_attempt_id: 2
  execution_retries_used: 1
  review_retries_used: 1
  result_ref: <final result ref>
  parameter_rejection: null # populated for parameter_recommendation_rejected_by_human
source_attempt_chain_hash: <hash of ordered attempt IDs + seal hashes>
```

Every entry requires sequence/identity links, attempt kind/origin, frozen
parameters, rendered command, execution outcome, review outcome when execution
succeeded and review ran, failure signal when failed, parameter delta,
inherited initial snapshot ref, approving human decision, and seal hash.
`terminal` is required for both success and failure. When a recommendation is
rejected before append, `terminal.parameter_rejection` records the proposed
attempt ID, task, previous attempt ID, recommendation ref/hash, decision ref,
and `reject_parameters`; no synthetic attempt entry is created. Rendering or
reloading the same sealed chain must produce the same semantic timeline and
`source_attempt_chain_hash`.

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
initial snapshot references, together with the exact human approval that
authorized execution. Failed attempts remain visible inside an ultimately
successful chain. A rejected initial or retry recommendation remains visible in
the terminal summary even though it never became an attempt.

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
parameter_recommendation_rejected_by_human
assembly_succeeded
assembly_failed
```

Execution exit/log/resource facts and review measurements remain structured
payloads, not classifier or repairability enums. The retry router uses only the
generic transition reasons `execution_failed`, `review_failed`,
`execution_retry_budget_exhausted`, `review_retry_budget_exhausted`,
`retry_delta_invalid`, `missing_failure_signal`,
`contradictory_failure_signal`, `unsealed_previous_attempt`,
`previous_attempt_seal_hash_mismatch`, `policy_snapshot_hash_mismatch`,
`parameter_recommendation_rejected_by_human`, and `completed`.

If the existing review recommendation contract supports stop or expert review,
those outcomes remain valid for review-origin requests with their existing
status/reason schema. They are not valid outcomes for an execution-origin
request while `max_execution_retries` budget remains. This design neither adds
a repairability policy nor invents a new stop/expert status. Schema validation
rejects unknown retry-routing values.

Valid terminal/transition pairings are closed as follows:

| Status | Allowed reason family | Counter effect |
|---|---|---|
| `retry_parameters_after_execution_failure` | `execution_failed` | Increment `execution` only after human approval, freeze, and attempt append |
| `retry_parameters_after_review_failure` | `review_failed` | Increment `review` only after human approval, freeze, and attempt append |
| `execution_parameter_retries_exhausted` | `execution_retry_budget_exhausted` | None |
| `review_parameter_retries_exhausted` | `review_retry_budget_exhausted` | None |
| `parameter_retry_validation_failed` | `retry_delta_invalid` | None |
| `parameter_retry_invalid_transition` | `missing_failure_signal`, `contradictory_failure_signal`, `unsealed_previous_attempt`, or `previous_attempt_seal_hash_mismatch` | None |
| `policy_snapshot_mismatch` | `policy_snapshot_hash_mismatch` | None |
| `parameter_recommendation_rejected_by_human` | `parameter_recommendation_rejected_by_human` | None |
| `assembly_succeeded` | `completed` | None |
| `assembly_failed` | Existing terminal reason outside an execution/review budget transition | None |

Tests must enumerate this pairing table, reject every invalid pairing, and
assert both counters before and after each accepted and rejected transition.

### Stage ownership and routing

| Owner | Input | May emit | Next route |
|---|---|---|---|
| Assembly initial parameter recommendation/validation | Initial query, selected top-5 evidence, and snapshotted policies | Validated recommendation and `parameter_approval` interrupt, or actual recommendation/validation error | Human gate; no freeze, append, or execution before approval |
| Assembly execution retry node (`app/stages/assembly/*`) | Execution-failed attempt with exactly-once raw structured execution result and failure signal | seal attempt; if budget remains emit retry request referencing its seal hash, otherwise emit execution exhaustion | Assembly parameter recommendation/freeze or terminal |
| Assembly parameter recommendation/validation (execution origin) | Typed execution retry request + attempt 0 snapshot | Validated recommendation and `parameter_approval` interrupt, or actual recommendation/validation error | Human gate; no freeze, append, increment, or execution before approval |
| Assembly parameter recommendation/validation (review origin) | Typed review retry request + attempt 0 snapshot | Validated recommendation and `parameter_approval` interrupt, or an existing review-contract terminal outcome | Human gate or existing review terminal route |
| Assembly `parameter_approval` human gate | Exact validated initial or retry recommendation | Immutable `approve_parameters` or `reject_parameters` decision | Approve: freeze/append, retry-only counter increment, execute. Reject: terminal rejection without execution or retry append |
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
  -> catalog/hard-rule/capability/parameter validation
  -> write exact recommendation ref/hash into parameter_approval interrupt
  -> block with supervision_state=blocked_human
  -> verify approve_parameters resume identity, chain head, and recommendation hash
  -> freeze approved parameters
  -> append attempt N+1
  -> increment only the owning domain counter
  -> execute
```

Any mutation or alteration of recorded failure facts/diagnosis after sealing is rejected. Recommendation
must not seal the previous attempt; it only verifies the request's seal hash.

| Condition | Required result |
|---|---|
| Initial recommendation validates | Block at `parameter_approval`; only `approve_parameters` may freeze attempt 0, append, and execute |
| Execution fails and execution budget remains | Execution owner writes raw structured facts/signal, seals, and emits a seal-bound request; recommend and validate, then block at `parameter_approval`; approval freezes/appends/increments/executes |
| Execution budget is exhausted | `execution_parameter_retries_exhausted` with the last execution failure |
| Execution retry recommendation or parameter validation errors | Preserve the error artifact and terminate through the existing recommendation/validation error route; do not append or increment; an attempted stop/expert action is a recommendation contract error, not a decision outcome |
| Execution succeeds and review passes | Finalize success |
| Review fails and review budget remains | Review owner writes diagnosis/signal, seals, and emits a seal-bound typed request; proposal is validated, then blocks at `parameter_approval`; approval freezes/appends/increments and routes back to assembly execution |
| Review budget is exhausted | `review_parameter_retries_exhausted` with the last review diagnosis |
| Failure signal is missing or contradictory | Fail closed as an invalid attempt transition; preserve artifacts |
| Review retry recommendation chooses stop/expert under an existing review contract | Preserve that existing outcome; do not append an attempt or consume review budget |
| Review retry parameter validation fails | Preserve the rejected recommendation artifact, do not append an attempt or increment review counter, and terminate that branch without execution |
| Human rejects an initial or retry recommendation | Record `reject_parameters`; terminate with status/reason `parameter_recommendation_rejected_by_human`; do not execute, append a retry attempt, or consume either retry budget |
| Resume targets wrong/stale attempt, replaced recommendation, mismatched hash, changed chain head, or resolved gate | Reject resume as an invalid transition; preserve the active valid interrupt when unresolved; do not freeze, append, increment, or execute |
| Approval is submitted more than once | The first valid decision is durable and authoritative; every duplicate is rejected with no additional side effect |

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
| Catalog/hard-rule validation and freeze | Retain | Automatic validation precedes human approval; freeze follows approval for every attempt |

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
| `app/review/base.py`, review gate registry, `app/control/lifecycle.py` | Reuse `ReviewPayload`, `ResumeReviewRequest`, `blocked_human`, durable decision artifacts, and resume dispatch; make only the minimal generic extension needed for exact recommendation/attempt/hash binding |
| `app/stages/assembly/routing.py`, `builder.py` | Preserve distinct execution and review nodes/routes; route every validated initial/retry recommendation through the shared `parameter_approval` gate; remove retry-selector assumptions |
| TUI and interactive presentation | No change | A later task wraps the generic `active_interrupt`; Task 6A/6B expose the contract only |
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
| Initial stable top-5 | More than 5 cases; ties at each lexicographic key; shuffled input order; clade and `case_id` tie-breaks; exactly 5, 1-4, and 0 eligible cases | Preserve Task 3 ordering exactly: `evidence_coverage` descending, `numeric_distance` ascending, existing clade tie-break, then `case_id`; select first 5, all 1-4, or existing no-case behavior for 0 |
| Initial no evaluation gate | Large numeric distances, low/missing coverage, any case count, absent evaluation, failing observational metrics, no recorded recommendation set | No distance/coverage/count/evaluation/scientific-approval threshold blocks selection or activation; the mandatory post-validation parameter human gate still controls execution |
| Observational evaluation | Run and omit evaluation; vary reported retrieval/recommendation metrics | Reports remain replayable when produced and never alter `initial_cutover_enabled`, selected cases, or control flow |
| Absence of retry retrieval | Config/schema rejection of retry backend; no retry query/case fixture; no retrieval call during retry | Retry works with no retry database and does not emit a retrieval artifact |
| Attempt 0 snapshot | Stable hashes/refs before and after retries; assert no catalog/knowledge pack/hard-rule/capability/profile/evidence reload; policy hash changes mid-run | Full basis is immutable; mismatch fails closed and requires a new run |
| Runtime resource isolation | Change current resource observations after the preceding execution result; replay the same retry; exercise safety veto | Frozen delta and rationale are identical; current observations can only block execution; resource deltas use only facts recorded in the immediately preceding execution result |
| Sealing lifecycle | Exactly-once result/diagnosis/signal population, overwrite rejection, post-seal failure-fact/diagnosis mutation rejection, artifact overwrite rejection, append-before-seal rejection, terminal-before-seal rejection, request seal-hash match/mismatch, serialization/reload | Owner seals before request; request binds the exact seal hash; membership is append-only and sealed values never change |
| Shared parameter approval payload | Initial, execution-retry, and review-retry validated recommendations | `active_interrupt` is `blocked_human`, uses `gate_id=parameter_approval`, identifies task/attempt/previous attempt, parameter diff, rationale, failure facts, evidence refs, recommendation ref/hash, and allows only `approve_parameters`/`reject_parameters` |
| Approval ordering and budgets | Assert state after validation, block, approve, freeze, append, counter increment, and execution for all three tasks | Nothing freezes/appends/executes before approval; retry counter increments only after approved freeze+append, and only in the owning domain |
| Resume gate identity | Valid request; omitted `gate_id`; wrong gate ID; unknown decision, each repeated with otherwise matching attempt/task/previous-attempt/hash metadata | Only an explicit `gate_id=parameter_approval` plus `approve_parameters` or `reject_parameters` is accepted; missing/wrong gate and unknown decisions fail closed before application even when all other identity fields match |
| Resume integrity and idempotency | Wrong attempt/task/previous attempt, stale or replaced recommendation, missing/wrong hash, changed chain head, duplicate approve, approve-after-reject, reject-after-approve, restart/reload | Every invalid or duplicate resume is rejected without freeze, append, counter change, or execution; one durable decision is authoritative across reload |
| Human rejection | Reject initial, execution retry, and review retry before append | Status and reason are `parameter_recommendation_rejected_by_human`; no command executes, no retry attempt appends, no retry budget is consumed, and the rejection appears in the terminal timeline |
| Execution retry routing | Raw exit/signal/log/resource fact variants; remaining/exhausted budget; successful recommendation; approve/reject; recommendation error; validation error; attempted model stop/expert; assert no classifier or repairability gate is invoked | With budget, every execution failure emits a request and every valid actionable parameter candidate validates then blocks for human approval; only approval freezes/appends/increments/executes; stop/expert fails the execution recommendation contract; exhaustion and rejection are domain-specific closed outcomes |
| Review retry routing | Every review reason/status, execution success + failed review, directional diagnoses, remaining/exhausted budget, approved/rejected proposal | Review stage owns diagnosis/request/terminal; every parameter proposal uses the shared gate; only a human-approved, frozen, appended review retry increments the review counter |
| Mixed chain | Execution failure -> execution retry -> review failure -> review retry -> pass | Both budgets remain independent and ordered attempts produce final success |
| Parameter delta validation | Changed, unchanged, coupled, unsupported, invalid-catalog, hard-rule violation | Every change is failure-linked and validated; unsafe/unsupported deltas never execute |
| Derived timeline | Successful, exhausted, and human-rejected integration chains under both `legacy_initial` and `curated_initial_v1`; required entry/terminal fields; approval refs/hashes; render/reload determinism; chain hash; missing/unsealed/unknown-contract/contradictory-input rejection | Timeline records the actual attempt 0 contract/snapshot and each execution-authorizing human approval; a rejected non-appended recommendation appears only in the terminal rejection summary |
| Candidate promotion removal | Search/config/schema and regression tests | No candidate `EvidenceCase` is emitted or promoted from runtime history |
| Legacy migration | Persisted-artifact/config inventory; idempotent read normalization when required, otherwise explicit no-data record; old schema/config rejection | Deleted retry evidence behavior cannot be re-enabled through legacy input |
| Regression | Other assemblers, existing typed lifecycle, full repository suite | No selector or retry behavior changes outside scoped hifiasm paths |

Task 6A is complete when `initial_cutover_enabled` activates the stable numeric
top-5 contract, including 1-4 and existing zero-case behavior, without an
evaluation or corpus gate, and a validated initial recommendation executes only
after the shared parameter human gate approves it.

Task 6B is complete when execution failures obligatorily progress to a validated
recommendation and the shared parameter human gate while execution budget
remains (except for actual recommendation/validation errors), approval alone
creates and executes the next attempt, rejection terminates without consuming
budget, review retains its independent budget and existing logic, and both
branches produce immutable, replayable timelines with domain-specific
exhaustion/rejection outcomes and no retry corpus, retriever, backend, or
candidate evidence promotion.

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
budget, mandatory execution retry while its budget remains, and only the
existing review-contract stop/expert outcomes on the review branch.

### Scope expansion

**Risk:** The split becomes a generalized retry framework or cross-assembler migration.
**Mitigation:** Reuse the current attempt lifecycle and stage nodes, add no new storage system, and keep Task 6A/6B hifiasm-scoped.

## Implementation Principle

Prefer deletion and reuse over abstraction. The smallest valid implementation keeps one initial evidence path, the existing typed attempt chain, and two independently owned retry transitions. Any proposed new class, store, backend, or artifact type must demonstrate that the existing attempt lifecycle cannot express the required invariant.
