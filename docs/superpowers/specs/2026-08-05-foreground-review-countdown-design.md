# Foreground Review Countdown Design

## Status

Approved for specification by the user on 2026-08-05. Implementation must not
preserve the superseded `strict` / `auto` review-mode system as a compatibility
layer.

## Problem

The public review policy is internally inconsistent. At the start of this task,
`RunConfig` only permitted stage overrides for Assembly, Assembly Review, and
Hi-C, while annotation gates carried separate hidden defaults. That defect
exposed the architectural problem: registered gates, including Inventory, T2T,
and parameter approval, do not share one policy path.

Annotation stages are being removed by a concurrent repository change. This
design preserves that removal and targets the current gate registry. A future
stage becomes covered by registering its gate; this change must not restore or
special-case removed annotation modules.

The intended operator experience is simpler: while the workflow process remains
in the foreground at a human gate, it shows an acceptance countdown. The
operator may claim the gate and make a human decision. If the countdown expires,
the gate applies its registered default acceptance decision. Exiting the process
leaves a durable blocked checkpoint and never schedules or wakes the workflow.

## Goals

- Replace `strict` / `auto` modes with one per-run wait duration.
- Apply the duration to every gate registered in `app.review.gates`.
- Reuse the existing gate registry, decision-template builder, synchronous
  timeout calculation, resume validation, resume transaction, and gate appliers.
- Keep all automatic decisions bound to the immutable request and recorded as
  `decided_by="auto_rule"`.
- Remove the old mode-driven policy, auto-decidability, pending-confirmation,
  and automatic LLM-review paths instead of deprecating them in place.
- Document the final contract in the owning `docs/architecture/` pages and
  public RunConfig examples.

## Non-Goals

- No scheduler, daemon, background thread, task wakeup, or cross-process timer.
- No automatic continuation after the foreground process exits.
- No manifest field for review timing; the manifest remains input and biological
  fact authority.
- No gate-level override. A stage override applies to every registered gate in
  that stage.
- No compatibility parser for `review_policy.mode`.
- No new biological thresholds or new decision vocabulary.

## Public Configuration

Review timing is owned by the per-task RunConfig:

```json
{
  "review_policy": {
    "max_blocked_seconds": 300,
    "stages": {
      "t2t": {"max_blocked_seconds": 0},
      "hic_scaffolding": {"max_blocked_seconds": -1}
    }
  }
}
```

The contract is:

- `-1`: wait indefinitely and never auto-accept;
- `0`: auto-accept immediately when the foreground gate loop sees the gate;
- a positive integer: display that gate's remaining countdown and auto-accept
  when it reaches zero;
- values below `-1` are invalid;
- the run-level default is `300` seconds;
- an omitted `review_policy` materializes the same `300`-second default in the
  frozen RunConfig;
- `stages` accepts only stages returned by `registered_review_stages()`;
- unknown fields, including `mode`, are rejected by the strict RunConfig schema.

Published RunConfig templates, examples, and TUI-authored RunConfig payloads
must show the default explicitly so an operator can change it for every task.
The source and frozen RunConfig remain the audit authority for the chosen value.

The existing duplicated `ReviewPolicyStage` literal is removed. Registry stage
validation is the only list of review-policy stages.

## Ownership And Persistence

`execute_frozen_session` resolves the effective run-level and per-stage values
from the frozen RunConfig and makes them available to every registered review
stage. It must iterate `registered_review_stages()` rather than a hard-coded
tuple.

`persist_review_request` stamps the effective `max_blocked_seconds` into the
immutable request record. The timing value participates in request identity, so
replaying the same gate under a different frozen policy cannot silently reuse a
request with different timing. Gate nodes do not evaluate a mode and do not
choose whether they are auto-decidable.

The compact interrupt retains its current request identity and `blocked_at`
timestamp. The large request body and timing authority remain in the verified
request artifact. Inspection may project the effective wait value, but reports
and checkpoints are not policy authority.

## Foreground Runtime Flow

The foreground behavior belongs at the existing `_interactive_gate_loop`
boundary, after the graph has persisted and checkpointed a normal
`blocked_human` result:

```text
graph checkpoints blocked_human
  -> interactive gate loop loads verified request and effective wait
  -> render the existing decision brief and registered default decision
  -> -1: enter the existing manual review prompt without a timer
     0: execute the registered default decision immediately
     N: show remaining seconds and wait for Enter or expiry
  -> Enter: cancel the timer and run the existing manual prompt
  -> expiry: build a bound auto_rule request and execute the existing
             resume transaction
  -> resumed graph runs until completion, failure, or the next gate
```

The countdown is a claim window. Once the operator presses Enter, it is
cancelled for that gate; time spent completing the existing multi-field prompt
does not cause an automatic decision to race or replace the human decision.

EOF, Ctrl-C, prompt cancellation, or process termination leaves the already
checkpointed session blocked. A later `gene-agent review` or `gene-agent resume`
is an explicit manual operation and does not auto-accept merely because the old
foreground deadline passed. The timer is not restored across processes.

`--non-interactive` retains its current behavior: persist the gate, print resume
guidance, and exit. It never starts a countdown.

The terminal wait implementation uses the Python standard library and an
injectable clock/input waiter. It must not add a production dependency. Output
updates in place at most once per second and names the stage, gate, default
decision, and remaining duration.

## Default Decision Reuse

Timeout decisions reuse the data and selection rules already used by
`build_resume_decision_template`. The common builder fills request identity,
parameter-attempt bindings, candidate IDs, candidate method, and resume metadata
from the verified request. It is shared by human decision templates and timeout
requests rather than copied into the interactive loop.

The registry's existing `template_preferred_decision` is the default acceptance
source. Missing acceptance defaults are completed as follows:

| Stage / gate | Timeout decision |
| --- | --- |
| Assembly / parameter approval | `approve_parameters` |
| Inventory / inventory review | `continue` |
| Assembly Review / assembly review | `accept` with the template-selected candidate |
| Assembly / route selection | existing timeout applier accepts the recommended route and method |
| Hi-C / parameter approval | `approve_parameters` |
| Hi-C / adoption review | `accept_candidate` |
| T2T / parameter approval | `approve_parameters` |
| T2T / finishing review | `accept_selected_candidates` with request candidate IDs |

The Assembly route-selection timeout applier remains because it already binds
the recommended route and method correctly. Other gates use the generic bound
default request. Human-only guards on Hi-C and T2T candidate adoption are
removed because `0` and finite expiry explicitly authorize the registered
automatic acceptance path.

An automatic request uses `decided_by="auto_rule"`, cites the immutable request
artifact as its rationale source, and records the configured duration, elapsed
duration, and `source="foreground_countdown"` in decision metadata.

## Existing Timeout Reuse

The implementation retains the existing synchronous timeout machinery:

- `_blocked_timeout_status` calculates expiry from `blocked_at`;
- `execute_resume_transaction` remains the single durable apply/checkpoint/
  invoke/audit boundary;
- `resume_review_state` continues to validate request and gate identity;
- `apply_review_gate_timeout` retains special gate timeout appliers;
- registered decision appliers continue to own domain mutation.

The foreground loop is the only automatic caller. Manual review/resume calls do
not infer a timeout from the request when no explicit internal timeout argument
is supplied. The public `resume --max-blocked-seconds` override is removed so a
post-freeze CLI flag cannot replace the per-task frozen RunConfig authority.

The generic timeout fallback changes from `stop_human_review` to the validated
registered default acceptance request. It must never fabricate a disallowed
decision.

## Failure And Cancellation Semantics

- If the request artifact or timeout value cannot be verified, leave the session
  blocked and display the error; do not auto-accept.
- If a default request cannot satisfy required bindings or candidate fields,
  leave the session blocked and enter or offer the normal manual review path.
- If timeout decision validation fails, record the existing resume failure event
  and leave the checkpoint blocked; do not convert it to terminal success or a
  stop decision.
- If a decision applier raises before a committed graph update, preserve the
  existing resume-transaction failure semantics.
- If the operator presses Enter before expiry, the human path wins and no
  timeout decision artifact is written.
- Replaying a committed automatic decision remains idempotent through existing
  request/decision bindings.

## Superseded Ecosystem Removal

Remove, rather than deprecate:

- `ReviewPolicyMode`, `mode` fields, mode merging, and mode-based defaults;
- `PolicyMode` and the `BLOCK_INDEFINITE`, `BLOCK_WITH_TIMEOUT`,
  `AUTO_DECIDE_NOW`, and `BLOCK_PENDING_CONFIRM` policy-action model;
- stage-local `evaluate(...)` calls and hidden `{ "mode": "auto" }` defaults;
- policy-driven `auto_decidable`, `pending_human_confirmation`, and
  `policy_mode` control/report fields where they have no independent consumer;
- the Assembly Review automatic LLM-recommender branch and its policy-only
  prompt, registry fields, tests, and documentation;
- `review_policy_outcome` action/mode payloads, replacing them with the single
  persisted wait value;
- tests and examples that assert or publish `mode: strict` / `mode: auto`;
- the public `resume --max-blocked-seconds` override.

Unrelated uses of `strict`, such as strict Pydantic parsing and the
`strict_t2t` biological target level, are outside scope and remain unchanged.
Historical Git commits are not rewritten.

## Documentation Changes

Update the current owning documents in the same implementation change:

- `docs/architecture/topics/review-and-resume.md`: foreground countdown,
  durable exit behavior, default-decision binding, and manual resume behavior;
- `docs/architecture/topics/runtime-orchestration.md`: interactive versus
  non-interactive control flow;
- `docs/architecture/reference/data-contract.md`: frozen RunConfig timing
  authority and request-artifact projection;
- `docs/architecture/reference/governance.md`: human gate policy wording;
- `docs/architecture/langgraph-implementation-map.md` if its status text
  describes old review behavior;
- public README, RunConfig examples, and configuration fixtures.

Do not add a second architecture owner or retain the superseded mode design in
current docs.

## Verification

Focused tests must prove:

- RunConfig defaults to `300`, accepts `-1`, `0`, and positive values, rejects
  values below `-1`, rejects `mode`, and validates stage keys from the registry;
- every registered gate receives an effective value and persists it in its
  immutable request;
- `0` auto-accepts through the normal resume transaction;
- a positive timeout renders a decreasing countdown and auto-accepts at expiry;
- Enter cancels the timer and the human decision wins;
- `-1` never starts automatic acceptance;
- Ctrl-C, EOF, cancellation, `--non-interactive`, and process exit leave the
  durable checkpoint blocked;
- starting manual review/resume later does not apply the expired foreground
  timer;
- each registered gate can build and validate its timeout request, including
  parameter bindings and required candidate IDs;
- Hi-C and T2T candidate adoption accept `auto_rule` only through the timeout
  path;
- adding a future registered review stage requires no RunConfig enum or
  `run.py` stage-list edit;
- no current review configuration, architecture document, example, or focused
  test retains `mode: strict` or `mode: auto`.

After focused tests, run the repository's configured formatting, lint, typing,
and full test gates. A command is reported as passing only after it exits zero
with its expected assertions.

## Acceptance Criteria

1. One frozen RunConfig value governs every registered review gate.
2. Default foreground behavior displays a 300-second acceptance countdown.
3. `-1`, `0`, and positive values have the exact semantics defined above.
4. The operator can claim a gate before expiry and complete the existing review
   prompts without a racing automatic decision.
5. Exiting or using `--non-interactive` never creates a background continuation.
6. Timeout acceptance uses existing registry, template, resume, persistence, and
   applier paths with complete audit evidence.
7. The old strict/auto review-mode ecosystem is absent from production review
   behavior, current docs, examples, and focused tests.
