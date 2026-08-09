# Review and Resume

> **Status:** implemented.
> **Read when:** changing a human gate, decision persistence, approval binding,
> resume validation, or post-review routing.  
> **Comes from:** a stage-owned immutable review request and compact blocked
> checkpoint.  
> **Feeds into:** [runtime orchestration](runtime-orchestration.md), a new
> parameter attempt, resource adoption, or terminal failure.  
> **Contract dependencies:** [run data](../reference/data-contract.md),
> [execution lifecycle](../reference/execution-lifecycle.md), and
> [stage contracts](../reference/stage-contracts.md).  
> **Code evidence:** `app/review/gates.py`, `app/review/persistence.py`,
> `app/review/approval.py`, `app/control/resume_transaction.py`, and
> `app/control/lifecycle.py`.

## Durable gate model

A human gate is a four-state durable protocol:

1. `awaiting_decision`: the stage has published an immutable review-request
   artifact and the checkpoint carries only a bounded interrupt cursor with
   request ref, request hash, `workflow_step_id`, stage, gate, and bindings;
2. `decision_committed`: a normalized `ResumeReviewRequest` has been written
   as the immutable review intent for that exact request identity;
3. `application_prepared`: the gate has prepared and published a typed review
   application artifact, including its verified decision artifact and required
   refs;
4. `applied`: a verified decision exists and either a checkpoint re-read proves
   that same verified application ref was projected or a proof-bound
   `review_applied` event records that completed checkpoint transition. When
   checkpoint proof exists but the event is missing, recovery backfills
   `review_applied` before the graph continues.

The prompt shown to a person is a projection of the request, not the recovery
authority. A checkpoint must not embed a full request, intent, decision, or
application body, and a Ledger event must not replace those artifacts.

Stage names are not review identities: a frozen plan can contain the same stage
more than once. Every review request, decision, Ledger event, and review path
is bound to the active occurrence's `workflow_step_id`; request and decision
artifacts live below `artifacts/steps/<workflow_step_id>/<stage>/reviews/`.
Resume rejects a request whose occurrence does not match the active frozen
workflow step.

Detached inspection follows the same authority boundary. If frozen workflow
inputs are missing, invalid, or disagree with the request's stage/occurrence,
it may expose the raw Ledger request for diagnosis but must suppress the
current occurrence, active interrupt, decision requirements, and actionable
review next step. An unverified Ledger projection cannot authorize resume.

The review registry centralizes gate declarations, allowed decisions,
occurrence validation, typed prepare/apply dispatch, and timing metadata. The
external resume DTO is one strict `ResumeReviewRequest` with a string decision
field. Normalization validates a proposal without writes; prepare owns durable,
gate-specific decision/evidence creation; verified apply is a pure projection
of the registered typed application into checkpoint state. Parameter approval,
assembly adoption, Hi-C adoption, and T2T adoption therefore remain different
gate-scoped vocabularies and effects without entrypoints owning a second apply
path.

## Foreground review policy

`RunConfig.review_policy.max_blocked_seconds` defaults to `300`. A frozen
configuration may override it per registry-backed review stage through
`review_policy.stages.<stage>.max_blocked_seconds`; an unregistered stage is
invalid. The effective value is copied into the immutable request so the gate
can be verified without consulting mutable current configuration.

The values have exactly these meanings during the original attached executor,
whether produced by structured `run` files or a confirmed TUI draft:

- `-1`: wait indefinitely for an operator decision.
- `0`: immediately normalize that gate's eligible automatic request, if any.
- a positive integer: display an Enter-claim countdown, then trigger the same
  normalizer if it expires.

The timer never supplies a default decision. Assembly Review retains its
evidence eligibility and bound LLM recommendation artifact; assembly route
selection uses its registered timeout-request normalizer. Assembly, AutoHiC,
and T2T parameter approval use the same registered normalizer to bind only the
recommendation in the immutable request. A gate with no eligible normalizer
remains manual. A failed automatic transaction may return to manual review only
while its `review_status` is `awaiting_decision` and the same verified
`blocked_human` request remains. Once intent is committed, the operator cannot
replace it: recovery re-enters the transaction with `request=None` and applies
that committed intent. Any identity or integrity drift fails closed. Enter
claims the gate, cancels the timer, and opens the normal manual prompts.
The timer is not a durable scheduler: Ctrl-C, EOF, or process exit leaves the
request and `blocked_human` checkpoint intact. Missing TTY capability prevents
a new run from freezing in the first place. There is no daemon or background wake-up.
Re-running the same frozen session detects the already-blocked checkpoint,
does not invoke it, and prints manual recovery commands. A later `review` or
`resume` is always a manual recovery action; it never reconstructs or expires
a countdown from the prior `blocked_at` time. Strict/auto review modes and a
public resume timeout override are not part of the interface.

### Example policy override

The three JSON examples
`docs/examples/assembly_review_run_config.template.json`,
`docs/examples/read_qc_assembly_review_run_config.template.json`, and
`docs/examples/repeated_stage_occurrences_run_config.template.json` deliberately
set the global default to `300` while setting
`review_policy.stages.assembly_review.max_blocked_seconds` to `-1`. JSON does
not permit comments: in each example this is an intentional per-stage override
that makes Assembly Review wait permanently for a human decision; it does not
inherit or eventually use the global 300-second countdown.

## Parameter-approval timeout behavior

The countdown begins only after a parameter recommendation has been parsed,
verified, persisted beneath its schema-3 manifest, and bound into an immutable
`parameter_approval` review request. It controls the default approval of that
one exact recommendation; it does not control recommendation generation,
modify parameters, request another recommendation, or choose registry defaults.

For every registered parameter-approval gate:

- `-1` never approves automatically and waits for a manual approve/reject
  decision;
- `0` immediately approves the exact persisted recommendation;
- a positive value displays an Enter-claim countdown, opens the manual
  approve/reject prompt when claimed, and approves the same recommendation at
  expiry.

One shared timeout-request normalizer enforces `decision=approve_parameters`
and `decided_by=auto_rule` and enriches audit metadata. It performs no state
or durable application mutation; the common intent/application protocol then
delegates preparation and verified pure apply to the registered stage gate.
Assembly, AutoHiC, and T2T retain their request, recommendation-manifest,
attempt-chain, predecessor, and workflow-occurrence validation without timeout
handling duplicating those stage rules.

The automatic decision binds the immutable review request and recommendation
hash and records the foreground-countdown source plus configured and elapsed
seconds. A claimed gate remains a human decision. If automatic normalization or
transaction preparation fails before intent commit and the durable checkpoint
still verifies as `blocked_human`, the same foreground terminal returns to
manual review. A committed intent is recovered, never replaced by a manual
decision. Broken request, evidence, checkpoint, or occurrence integrity fails
closed instead of fabricating approval.

## Resume validation

Before state changes, resume reloads the current checkpoint and verifies the
session, workflow-step identity, stage, gate, request ID and hash, decision
vocabulary, manifest identity, and any recommendation/attempt bindings. Missing, stale,
cross-session, conflicting, or already-consumed input fails closed. Republishing
the identical immutable bytes is idempotent, but it does not authorize applying
an already-consumed gate a second time.

Resume does not use elapsed wall time from a previous foreground invocation.
It waits for the supplied manual decision (interactive `review` or a verified
decision file) and never turns a durable block into an automatic approval.
A countdown-triggered automatic decision is accepted only when the resume
transaction's trusted source and hash-bound countdown metadata identify the
active `foreground_countdown`. The decision origin and rationale still come
from the gate's resolver (`llm_provisional` or `auto_rule`); a manual `review`
or `resume` source cannot impersonate an automatic timeout even if a decision
file claims otherwise.

The resume flow is:

```text
acquire the active FrozenExecutionLease
  -> read checkpoint and project the shared review status
  -> normalize the supplied proposal only when status is awaiting_decision
  -> persist immutable intent
  -> gate prepare creates typed decision/evidence effect
  -> persist and verify immutable application
  -> verified gate apply returns a pure compact state update
  -> graph.update_state(...)
  -> re-read the checkpoint and prove application in the checkpoint
  -> record_review_applied
  -> graph.invoke(None, ...)
  -> finalize the authoritative terminal singleton when reached
  -> record resume completion or failure
```

This is an idempotence-aware saga across artifacts, Ledger, and SQLite
checkpoint operations. It is not an ACID transaction spanning those stores.
Each durable step is replay-aware and validates the evidence left by earlier
steps. The shared status projection is the recovery decision point: a later
call with `request=None` recovers a committed/application-prepared intent,
whereas a replacement decision is legal only in `awaiting_decision`.

## Decision effects

A decision may approve parameters, accept or reject a biological candidate,
keep a prior resource, authorize a bounded retry, or stop. It must not silently
create an unregistered method or bypass the frozen-attempt boundary.

Terminal completion or failure may trigger report generation and checkpoint
compaction. Immutable review requests, decisions, execution evidence, and
adopted biological artifacts remain retained independently of checkpoint
history.
