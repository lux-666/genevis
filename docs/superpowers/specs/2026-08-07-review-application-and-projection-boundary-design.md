# Review Application And Projection Boundary Design

## Status

Direction approved by the user on 2026-08-07. The user selected the complete
design and added one governing constraint: reuse existing durable components
where they fit, but replace an unsuitable interface completely rather than
leaving compatibility aliases, fallback dispatch, or parallel old/new paths.

Independent specification review passed on the third review round. The document
is awaiting the user's final written-spec review before implementation.

## Problem

The current review resume path and the current presentation path each cross an
ownership boundary in the wrong order.

Review resume currently executes a gate-specific applier before updating the
LangGraph checkpoint:

```text
validate blocked checkpoint
  -> gate applier writes decision.json
  -> gate applier records review_decision
  -> gate applier may publish additional durable biological effects
  -> graph.update_state(...)
```

If `graph.update_state()` fails or has an ambiguous outcome, immutable review
facts can say that a request was decided while the checkpoint remains
`blocked_human`. Existing inspection, Ledger validation, reports, and traces
mostly treat `review_decision` as proof that the request is closed. The same
decision is often accidentally replayable because the artifact and Ledger
writers are idempotent, but there is no explicit same-decision recovery
protocol and no durable marker proving that the checkpoint applied the
decision.

The parent controller also performs non-authoritative presentation work inside
the graph node. It records `session_terminal:completed` and then calls an
unprotected progress renderer. A renderer exception can escape `graph.invoke`,
be normalized as a workflow failure, and append `session_terminal:failed` for
the same session. Operator-report failures can similarly turn a blocked state
into a failed state or obscure the original failure.

These are not two unrelated exception checks. They are ownership defects:

- review is a recoverable domain transaction;
- progress and reports are best-effort projections with no lifecycle control;
- a session terminal outcome is a singleton authoritative fact.

## Goals

- Make every registered review gate recoverable across the immutable-artifact,
  Ledger, and SQLite-checkpoint boundary.
- Commit the submitted decision before any gate-specific durable preparation,
  then require replay of that exact committed decision.
- Separate gate-specific durable preparation from pure checkpoint application.
- Make checkpoint application explicitly provable through an application ref
  and a `review_applied` Ledger event.
- Serialize every graph-mutating run/resume operation through the existing
  per-session frozen-execution lock and revalidate the checkpoint immediately
  before applying an application.
- Give lifecycle inspection, Ledger checks, operator reports, and traces one
  shared review-status projector.
- Isolate progress, operator-report, and final-report exceptions from workflow
  state and terminal outcome.
- Enforce at most one semantic terminal result per session at the Ledger write
  boundary.
- Convert all eight registered gates in one implementation. The finished tree
  must have no legacy `decision_applier` or effectful timeout-applier path.
- Reuse existing idempotent artifact publication, artifact-index registration,
  Ledger locking/idempotency, checkpoint compaction, report writers, and
  gate-specific evidence validators.

## Non-goals

- No generic saga, workflow engine, transactional outbox, or event bus.
- No ACID transaction spanning the filesystem, Ledger JSONL, and SQLite.
- No migration to `graph.stream()` in this change. Attached progress may remain
  at the parent-controller transition, but it must run through the projection
  isolation boundary.
- No attempt to make every operator report fully regenerable from durable
  records. The existing report-regeneration conformance debt remains explicit.
- No rewrite of append-only historical Ledger rows.
- No opportunistic schema cleanup, strict RunConfig typing change, unrelated
  stage refactor, or new production dependency.

## Existing Components To Reuse

The design deliberately builds on current repository mechanisms:

- `persist_review_decision()` remains the canonical immutable `decision.json`
  publisher.
- `publish_artifact_record()` and the artifact index retain their same-path,
  same-bytes recovery and conflict behavior.
- `publish_or_recover_work_artifact()` remains the normal helper for work that
  may have been published before a process stopped.
- deterministic derived-candidate and stage-evidence publishers remain owned by
  their stages and are called during review preparation.
- `Ledger.record_event()` remains the append-only, locked, idempotent event
  primitive; typed review and terminal methods tighten its domain contracts.
- `recover_execution_result()` and artifact publication recovery are design
  precedents, not new parallel review implementations.
- `compact_terminal_checkpoint()` remains the terminal checkpoint compactor.
- `write_operator_report()`, `write_final_review_report()`,
  `write_session_trace()`, `source_watermark()`, and `atomic_write_text()` remain
  low-level projection writers.
- the cursor calculation in `_render_attached_progress()` remains the attached
  progress renderer after relocation behind the projection boundary.

Where a current function mixes validation, publication, Ledger writes, and a
`StateUpdate`, its internal validators and publishers are reused, but the mixed
entry point is removed after migration.

## Review Application Lifecycle

### Durable records

Each review request occurrence gains three immutable records under its existing
review directory:

```text
workflow/<step>/<stage>/reviews/<review_request_id>/intent.json
workflow/<step>/<stage>/reviews/<review_request_id>/decision.json
workflow/<step>/<stage>/reviews/<review_request_id>/application.json
```

`intent.json` is the first persistence boundary. It contains the strictly
validated, canonical `ResumeReviewRequest`, request identity and hash, source,
trusted foreground-countdown facts when applicable, and rationale ref. It does
not contain a volatile timestamp. Replaying identical canonical bytes recovers
the existing intent. Different bytes for the same request produce a dedicated
decision-conflict error and never reopen prompting.

`decision.json` keeps its current meaning: the gate's normalized domain
decision evidence. Gate preparation may publish it early when later artifacts
need its identity, but it is not proof that the checkpoint advanced.

`application.json` is the commit marker for completed durable preparation. Its
generic envelope contains:

- schema version and deterministic application ID;
- session, workflow-step, stage, gate, and review-request identity;
- request hash plus `intent_ref` and `decision_ref`;
- one registered `effect_kind`;
- a bounded, gate-specific typed effect payload;
- refs to every durable effect required before checkpoint application.

It contains no arbitrary `StateUpdate`, embedded artifact bodies, generated
timestamps, or second copy of the checkpoint. The application ID is derived
from canonical envelope content excluding the `application_id` field itself:

```text
application_id = "review_app_" + sha256(
  canonical_json(application_envelope_without_application_id)
)[:24]
```

The verified loader removes that field, recomputes the same preimage, and
rejects either a changed ID or a changed preimage field.

The wire contract is explicit rather than implied by Python dictionaries:

- `ReviewIntentV1` is a versioned Pydantic model with `extra="forbid"`;
- `ReviewApplicationV1` is a versioned generic envelope with
  `extra="forbid"`;
- every registered `effect_kind` names one versioned gate-owned Pydantic effect
  model and decoder;
- the registry binds a gate to exactly one accepted `effect_kind` and decoder;
- JSON uses sorted keys, UTF-8, `allow_nan=False`, and fixed size limits;
- path-like values are converted to verified run-relative refs;
- each effect schema declares whether a list is ordered or set-like, and
  set-like values are normalized deterministically before persistence.

`ResumeReviewRequest.metadata` is not copied unchecked. Intent construction
first validates that the complete canonical request is bounded JSON and that
its gate-specific metadata contains only the registered fields and portable
refs accepted by that gate. Existing request validation remains the first
layer; intent validation is the durable wire-contract layer.

Recovery never recomputes an existing application in order to replace it. If
`application.json` exists, the coordinator loads and verifies those bytes using
the registered decoder. Gate preparation runs only while no valid application
marker exists. Volatile values needed by a durable effect, such as an attempt
seal timestamp, are either anchored in the committed intent or loaded from an
already-published effect; they are never regenerated differently on replay.

The Ledger gains a typed `review_applied` event. `review_decision` records that
the normalized decision and prepared application exist; it no longer closes
the request. `review_applied` is recorded only after rereading the checkpoint
and verifying that the exact `application.json` ref was committed with the
gate update.

### State marker

`TopLevelState` and `StateUpdate` gain one compact optional
`review_application_ref` field containing only the latest applied
`ArtifactRef`. Gate application writes this ref in the same
`graph.update_state()` call as the gate-specific update and interrupt removal.

The coordinator verifies the ref by calling `graph.get_state()` after every
update attempt, including attempts that raised or returned an ambiguous result.
Because LangGraph commits one channel update as a checkpoint operation, a
matching application ref proves that the rest of that same update crossed the
checkpoint boundary. A later review may replace the field; recovery always
checks the ref's request and occurrence identity.

The state schema validates the structural `ArtifactRef` shape. The field is not
trusted merely because it is well-shaped. A shared
`load_verified_review_application(paths, ref, expected_identity=...)` helper
must verify the indexed artifact, bytes/SHA, application schema, application ID,
session, workflow step, stage, gate, request ID/hash, intent ref, decision ref,
and registered effect kind. The coordinator, status projector, checkpoint
reconciliation, and typed `record_review_applied()` API all use this loader.
`review_applied` carries the verified application ref and uses the application
ID in its fixed idempotency key.

### Gate contract

`ReviewGateDeclaration` is replaced atomically. The final declaration contains
these behavioral hooks:

```text
prepare_application(state, intent) -> PreparedReviewApplication
apply_application(state, prepared_application) -> StateUpdate
normalize_timeout_request(state, request, timeout) -> ResumeReviewRequest  # optional
```

`prepare_application` owns gate-specific validation and idempotent durable
preparation. It may reuse current publishers for decision evidence, derived
biological artifacts, candidate bundles, attempt seals, and other required
effects. It must be recover-first: an identical intent either verifies and
reuses earlier effects or republishes missing idempotent facts. It returns a
typed prepared application only after all required effects exist.

`apply_application` is pure with respect to durable storage. It accepts the
blocked checkpoint state plus the typed application and computes a compact
`StateUpdate`. It performs no filesystem write, artifact-index registration,
Ledger append, report generation, subprocess execution, LLM call, or wall-clock
read.

`normalize_timeout_request` replaces the current effectful timeout-applier
interface. It may verify immutable request evidence and derive the canonical
automatic request, but it performs no writes. The resulting request is
persisted in `intent.json` and follows the same preparation and application
path as a human decision.

Each declaration also registers its exact `effect_kind` and effect-model
decoder. `apply_application` receives a decoder-validated typed model, never an
unchecked mapping loaded from JSON.

The old fields and functions are removed, not deprecated:

- `ReviewGateDeclaration.decision_applier` and `.timeout_applier`;
- `load_decision_applier()`, `load_timeout_applier()`,
  `apply_review_gate_decision()`, and `apply_review_gate_timeout()`;
- the effectful `resume_review_state()` dispatch path;
- direct gate entry points named `apply_*_resume_decision` or equivalent;
- compatibility aliases or registry fallback to the old callables.

### Coordinator ownership

`app/review/application.py` owns the review-specific records, typed envelopes,
verified loaders, and recovery helpers. It is not a general saga framework.
`execute_resume_transaction()` remains the single control-layer coordinator
used by structured run, interactive review, and explicit resume.

### Execution lease and stale-checkpoint protection

Every graph-mutating path runs under the existing per-session
`frozen_execution_lock`. The context manager is extended to yield a
non-serializable `FrozenExecutionLease` naming the session and run directory.
`execute_resume_transaction()` requires this lease and rejects a mismatched or
inactive lease. The initial structured/TUI execution keeps its existing lock
across its in-process review loop. Standalone `review` and `resume` acquire the
same lock before graph construction and transaction execution. Read-only
inspection does not acquire it.

Lease acquisition has one API and is never nested. An outer owner passes the
same lease explicitly through `_execute_verified_frozen_session()`, the
foreground gate loop, TUI review, and `execute_resume_transaction()`. A public
review/resume entry point accepts an optional lease: when present it validates
and reuses it; when absent it acquires exactly once and passes it to its private
body. The transaction itself never acquires a lock and has no no-lease bypass.
This prevents the current TUI path, which already holds `flock`, from trying to
take the non-reentrant lock a second time.

A standalone review may collect human input before acquiring the lease, but the
coordinator treats it only as a proposal. After acquiring the lease it reloads
the checkpoint and durable status before persisting intent, so a stale prompt
cannot mutate the session.

The lease prevents two coordinators from invoking the graph concurrently. As a
second defense, immediately before `graph.update_state()` the coordinator
rereads the checkpoint and verifies one of two legal cases:

- it is still the exact blocked request occurrence named by the application;
- it already carries the exact verified application ref, in which case no
  application update is repeated.

A different request, occurrence, terminal state, or later checkpoint causes
reconciliation/exit, never application of an old update to new state.

### Coordinator algorithm

The coordinator executes this algorithm while holding the execution lease:

```text
0. Read the current checkpoint and canonical terminal fact before accepting any
   review mutation.
   a. Ledger terminal + matching terminal checkpoint:
        do not persist review records or invoke; run terminal reconciliation and
        projections only.
   b. Ledger terminal + nonterminal/mismatched checkpoint:
        report a legacy terminal-checkpoint integrity error; do not rewrite the
        checkpoint, persist review records, or rerun a stage.
   c. no Ledger terminal + verified terminal checkpoint:
        complete terminal finalization from that checkpoint; do not invoke.
   d. neither is terminal:
        continue review recovery.
1. Project durable review status for the current checkpoint occurrence.
2. Branch by checkpoint/review state:
   a. blocked request -> verify active_interrupt and occurrence identity;
   b. checkpoint carries this verified application ref but review_applied is
      missing -> skip prompt, prepare, and apply; repair review_applied;
   c. applied nonterminal checkpoint -> continue invocation;
   d. any unrelated/later state -> reconcile or fail closed without old writes.
3. For an exact blocked request with no intent:
     validate/normalize the supplied decision;
     persist intent.json.
   Else:
     load the committed intent;
     reject a conflicting supplied decision;
     continue from the committed intent without prompting.
4. If no application exists:
     call the registered prepare_application(state, intent);
     persist/recover application.json as the final preparation marker.
5. Persist/recover the canonical review_decision Ledger event from the
   application metadata.
6. Reread and revalidate the checkpoint immediately before application.
   If its review_application_ref does not match:
     require the same blocked request occurrence;
     compute the pure gate StateUpdate;
     add review_application_ref;
     call graph.update_state(...);
     reread the checkpoint even when update_state raises or returns false.
7. If the checkpoint now carries the exact application ref:
     persist/recover review_applied.
   Otherwise:
     stop with a recoverable checkpoint-application error.
8. Continue graph.invoke(None, ...).
9. Reconcile authoritative terminal/checkpoint state.
10. Run best-effort projections and return bounded warnings.
```

If application is already prepared, the coordinator never reruns prompting or
chooses a new decision. If checkpoint application is already proven but
`review_applied` is absent, it records the missing event without repeating gate
effects or requiring `blocked_human`/`active_interrupt`. If review is applied
but invocation stopped, it resumes invocation. If the session is already
terminal, step 0 prevents every new review write and only reconciles terminal
bookkeeping and regenerable projections.

`execute_resume_transaction()` accepts an optional request during recovery. An
awaiting request still requires operator input. A committed intent can be
recovered without resupplying a decision file because the trusted decision is
loaded from `intent.json`.

### Foreground automatic fallback boundary

The automatic countdown loop may fall back to a fresh manual decision only when
the current request still has no committed intent. Once `intent.json` exists,
the loop must recover that exact automatic decision. It may retry immediately
or leave the session blocked with an explicit recovery action, but it must not
prompt for a different decision.

`ResumeTransactionError` therefore reports the named phase plus the projected
review status. Callers do not infer safety from a broad phase such as
`decision_apply`; they ask whether the request is still `awaiting_decision`.

## Registered Gate Migration

All eight declarations move to the new contract in the same implementation:

| Gate | Preparation responsibility | Pure application responsibility |
|---|---|---|
| `assembly/parameter_approval` | verify recommendation chain; publish decision evidence | update attempt, cursor, retry counts, or terminal rejection |
| `inventory/inventory_review` | verify frozen inputs; prepare/recover registry, candidate, and decision artifacts | commit inventory cursor/resources or terminal stop |
| `assembly_review/assembly_review` | prepare/recover selected action, derived candidate, decision, and action evidence | update review cursor/resource and advance, or terminal stop |
| `assembly/route_selection` | normalize route selection and publish decision | update assembly selection cursor or terminal stop |
| `hic_scaffolding/parameter_approval` | verify recommendation chain; publish decision evidence | update AutoHiC attempt/cursor or terminal rejection |
| `hic_scaffolding/hic_scaffolding` | prepare/recover decision, derived candidate, and retry-attempt seal | update adoption/retry/resource state or terminal stop |
| `t2t/parameter_approval` | verify recommendation chain; publish decision evidence | update T2T attempt/cursor or terminal rejection |
| `t2t/t2t_review` | prepare/recover patch materialization, scans, final evidence, and decision | update T2T cursor/resource and advance, or terminal stop |

The three parameter-approval gates share one typed preparation helper for the
common request, recommendation, manifest, audit, and decision payload. Their
stage modules retain only stage-specific cursor validation and pure update
construction.

Misleading current names are corrected during migration. In particular,
helpers called `prepare_*` that currently write attempt manifests or artifacts
are split or renamed so the final names state whether they perform durable
preparation or pure application.

After migration, stage modules may still call approved idempotent publication
helpers from `prepare_application`, but no stage application function may call
`persist_review_decision`, `Ledger.record_review_decision`, report writers, or
other durable sinks. A conformance test enforces the removed registry fields
and direct-call surface.

## Shared Review Status Projector

`app/review/status.py` becomes the only projector that decides whether a review
request is open or applied. It emits exactly four lifecycle states:

| Status | Durable meaning | Allowed action |
|---|---|---|
| `awaiting_decision` | verified request, no intent | accept one new decision |
| `decision_committed` | intent exists, application absent | replay committed intent preparation |
| `application_prepared` | application exists, application not yet proven in checkpoint | apply/reconcile exact application |
| `applied` | `review_applied` or equivalent verified later progression | do not prompt; continue execution/projection |

Corrupt refs, conflicting immutable records, or inconsistent identities are
reported as bounded integrity errors attached to the status; they do not create
a fifth permissive state.

For records created after this change, `review_applied` is the normal closing
fact. For historical completed runs, a later stage occurrence, terminal event,
or other durable progression that could only follow the gate is accepted as
equivalent applied evidence. A legacy `review_decision` by itself is never
enough. A pre-change partial session with only a decision and a still-blocked
checkpoint fails with an explicit legacy-recovery diagnostic; the runtime does
not keep a permanent legacy execution path whose semantics cannot be proved.

Every request is keyed by the full identity
`(workflow_step_id, review_request_id, request_hash)`. Historical progression
counts only when its Ledger sequence is later than the request, the frozen
linear plan proves that the progression passed that exact occurrence, and no
canonical terminal precedes the request. Repeated stage names, an unrelated
later event, or a post-terminal `review_requested` row cannot close or reopen a
request. Canonical terminal selection runs before review projection; rows after
terminal are integrity diagnostics only.

The following consumers stop implementing their own resolved test and use the
shared projector:

- `inspect_session()` and lifecycle next-action construction;
- `Ledger._latest_open_review_request_id()` or its replacement typed helper;
- `_pending_review_requests()` and operator/final review reports;
- session trace stage/status projection;
- TUI durable-state decisions;
- resume and foreground fallback eligibility.

The detached inspect path remains a durable-record read model. It does not
pretend to decode a LangGraph checkpoint summary. Documentation changes its
wording to “SQLite checkpoint availability/progress marker.” Recovery commands
that must reconcile the actual checkpoint continue to construct the verified
graph and call `graph.get_state()`.

## Projection Boundary

`app/control/projections.py` introduces one runtime boundary for all
non-authoritative presentation callbacks:

```text
ProjectionBoundary.run(name, callback, *args, disable_after_failure=False)
  -> ProjectionResult
```

The boundary:

- catches `Exception`, never `BaseException`;
- bounds the error type and text;
- returns a structured warning instead of raising into graph/control flow;
- never modifies graph state, checkpoint state, terminal outcome, or biological
  artifacts;
- disables a named progress sink after its first failure when requested;
- accumulates warnings for the CLI/TUI to render without changing lifecycle
  exit semantics.

The boundary does not expose the graph, checkpointer, Ledger, or mutable
`TopLevelState` to an attached renderer. It derives a frozen
`ProgressProjectionInput` containing only scalar counts, immutable identifiers,
stage, and presentation state. The renderer's return value is ignored. Report
writers run after invocation against a defensive deep copy of the returned
state. Mutating a callback argument therefore cannot mutate the routed state or
checkpoint.

The parent controller keeps the existing transition hook, but invokes it only
through this boundary. This deliberately avoids a riskier `graph.stream()`
conversion. A progress callback may observe a transition after its Ledger
occurrence event and before the graph node returns; its failure is harmless
because the boundary absorbs it.

`write_operator_report()` is removed from `app/graph/builder.py`. After
`graph.invoke()` or a resume invocation returns and authoritative terminal
finalization succeeds where applicable, the executor projects:

- `blocked_human` -> current operator report;
- `failed_terminal` -> failed operator report;
- `completed` -> final session report.

The same projection helper is used by initial run, foreground resume,
`gene-agent review`, and `gene-agent resume`. Projection warnings are printed to
stderr and returned in transaction results where applicable. A completed run
with a failed final report remains completed and returns the normal successful
lifecycle code. A blocked report failure remains blocked. A failed report
failure preserves the original workflow error.

`app/graph/builder.py` no longer writes `session_terminal`; it retains only
occurrence facts such as stage entered/completed/failed. This removes the
current Ledger-before-checkpoint terminal window.

`finalize_terminal_state()` is replaced by one authoritative terminal finalizer
used by initial execution and every resume path. It performs this order:

```text
1. Obtain the terminal state returned by graph.invoke, or build the normalized
   failed state for an invoke exception.
2. For a normal terminal return, reread and verify that the current checkpoint
   is the same completed/failed terminal state.
   For a normalized exception, graph.update_state the failed terminal fields,
   then reread and verify them.
3. Only after terminal checkpoint proof, call the singleton
   Ledger.record_session_terminal().
4. Run best-effort terminal checkpoint compaction.
```

After the authoritative finalizer returns, the executor runs final/operator
reports through `ProjectionBoundary`.

If the process stops after step 2 and before step 3, the next lease holder sees
a terminal checkpoint without a Ledger terminal and completes finalization
without invoking a stage. If it stops after step 3, both durable authorities
already agree and only compaction/projection may be retried. If checkpoint proof
fails, no terminal Ledger fact is written.

The authoritative finalizer performs no report write. The old mixed
function, builder terminal writes, and the resume-only `terminal_report` error
phase are deleted.

## Single Terminal Outcome

`Ledger.record_session_terminal()` becomes the sole public writer for
`session_terminal`. Generic `record_event(..., "session_terminal", ...)` is
rejected so callers cannot bypass the singleton rule.

The typed writer accepts only `outcome: Literal["completed", "failed"]`, removes
the caller-supplied idempotency key, and checks/appends under the same Ledger
file lock:

1. no existing terminal -> append one terminal with a fixed per-session key;
2. one terminal with the same outcome -> return the first immutable row without
   appending or replacing its stage/error/refs diagnostics;
3. one terminal with a different outcome -> raise `LedgerConflictError` and
   append nothing;
4. multiple historical terminals -> report integrity conflict and append
   nothing.

Callers cannot override the terminal idempotency key. All current outcome-based
keys are removed.

Read paths do not rewrite append-only history. For a legacy session containing
multiple terminal rows, the first terminal event is the canonical irreversible
outcome. Later rows are exposed through a `terminal_integrity` diagnostic and
cannot replace that outcome. `inspect_session()`, trace generation, checkpoint
compaction, and storage audit use the same canonical-terminal selector. A
terminal session cannot be projected back to blocked because a malformed later
review request exists.

When graph execution raises, the failure normalizer first checks the canonical
terminal. If a terminal already exists, it preserves that outcome and reports
the execution/checkpoint exception as an operational warning. Only a run with
no terminal fact may be normalized to a new failed terminal.

Canonical terminal selection is also the first check in every review
coordinator path. A stale blocked SQLite checkpoint cannot authorize review
writes after the Ledger has reached terminal.

## Error Handling

Review transaction phases become explicit:

- `snapshot` / `validation`;
- `intent_persist`;
- `application_prepare`;
- `application_persist`;
- `checkpoint_apply`;
- `checkpoint_verify`;
- `invoke`;
- `terminal_finalize`.

Every failure records the current review status and durable refs in bounded
resume audit metadata. The error never authorizes a new decision after intent
commit.

Projection failures are not resume phases. They produce `ProjectionWarning`
values and cannot create `resume_failed`, `failed_terminal`, or another terminal
row.

Application and intent conflicts fail closed with domain-specific diagnostics
that name the immutable ref. Raw `FileExistsError`, artifact-index conflicts,
or generic idempotency-key errors are not exposed as normal operator guidance.

## Documentation Changes

The implementation updates the architecture documents together with code:

- `review-and-resume.md` describes intent, application, checkpoint proof,
  `review_applied`, and the pre-intent-only manual fallback boundary.
- `runtime-orchestration.md` describes projection warnings instead of terminal
  finalization errors and changes “checkpoint summary” to “SQLite checkpoint
  availability/progress marker.”
- `data-and-storage.md` and `system-map.md` retain reports as projections with no
  control authority and identify review application records as durable facts.
- `conformance.md` records projection-exception lifecycle isolation as closed
  while retaining the separate durable-record report-regeneration debt.
- the Assembly Review example documentation explicitly says its
  `max_blocked_seconds: -1` override is intentional: it enters foreground manual
  review without an automatic expiry decision and does not create a persistent
  background waiter.

## Test Strategy

### Review recovery matrix

Tests cover each durable boundary with a real registered gate rather than only
mocking `ResumeTransactionError`:

- no intent -> new decision accepted;
- intent only -> same intent preparation resumes without prompting;
- partial durable preparation -> idempotent effects recover;
- application present, checkpoint blocked -> exact application is reapplied;
- `update_state()` raises after committing -> checkpoint reread recognizes the
  application ref and records `review_applied`;
- checkpoint carries application ref, Ledger event missing -> event is repaired
  without requiring an interrupt or repeating prepare/apply;
- applied checkpoint, invoke interrupted -> graph invocation continues;
- terminal outcome, report missing -> only the projection is retried;
- conflicting decision after intent -> explicit immutable conflict and no
  writes;
- automatic failure before intent -> manual fallback allowed;
- automatic failure after intent -> no manual prompt, same intent recovery.
- two concurrent recovery processes -> one execution-lease winner; the loser
  performs no apply/invoke and cannot apply an old application to a later gate;
- direct review, CLI resume, and TUI nested review each acquire or propagate
  exactly one lease; the TUI never attempts a second non-reentrant `flock`;
- canonical terminal plus stale blocked checkpoint -> no intent/application or
  graph mutation is permitted.

At least one parameter-approval gate and one write-heavy biological gate use a
real failure-window test. A registry-wide contract test verifies that all eight
gates expose only the new hooks. Focused stage tests verify each typed effect
payload and pure application result.

### Status/read-model tests

- all four statuses are projected from durable combinations;
- `review_decision` without application proof remains recoverable, not resolved;
- bad SHA, unindexed application ref, mismatched occurrence identity, or wrong
  effect kind fails closed in every trust consumer;
- a changed application ID or any changed hash-preimage field is rejected by
  the verified loader;
- lifecycle, Ledger open-request checks, report pending flags, trace status, and
  TUI next actions agree on the same request status;
- normal historical progress proves old completed requests applied;
- an unprovable legacy partial decision yields a bounded recovery diagnostic.
- repeated stage names, unrelated later events, and post-terminal review rows do
  not incorrectly close or reopen a request.

### Projection and terminal tests

- progress renderer raises `BrokenPipeError`, `OSError`, `ValueError`, and a
  generic `RuntimeError`; workflow state and terminal outcome do not change;
- progress renderer attempts to mutate nested callback input; routed state and
  checkpoint remain unchanged because it receives only an immutable snapshot;
- the first progress failure disables that sink for later transitions;
- blocked operator-report failure remains `blocked_human`;
- completed final-report failure leaves exactly one completed terminal;
- failed operator-report failure leaves exactly one failed terminal with the
  original error;
- completed and failed terminal flows inject a stop after checkpoint proof but
  before Ledger terminal, and after Ledger terminal but before projection;
  recovery writes no second checkpoint transition and never reruns a stage;
- initial run, foreground resume, explicit review, and explicit resume all use
  the same projection path;
- same-outcome terminal retries return the first row even if later diagnostics
  differ; completed-versus-failed concurrent writers produce one row and one
  deterministic conflict;
- generic `record_event("session_terminal", ...)` and unsupported terminal
  outcomes are rejected;
- a legacy completed-then-failed Ledger projects completed as canonical plus an
  integrity warning and refuses a third terminal.

### Removal and regression checks

- no stage imports/calls `Ledger.record_review_decision` from a pure application
  function;
- no `ReviewGateDeclaration.decision_applier`/`timeout_applier` or legacy
  dispatch helper remains;
- no report writer or terminal writer is called from `app/graph/builder.py`;
- existing artifact integrity, checkpoint compaction, review occurrence,
  parameter recommendation, and report watermark tests remain green;
- the focused suites run first, then the repository's required full test and
  static-check commands must complete successfully.

## Implementation Shape

The intended final ownership is:

```text
app/review/application.py
  intent/application persistence, recovery coordinator helpers, typed envelope

app/review/status.py
  sole open/committed/prepared/applied projector

app/review/gates.py
  static registry of prepare/apply/timeout-normalizer hooks

app/stages/... review preparation
  gate-specific validation and idempotent durable effects

app/stages/... pure application
  typed application -> compact StateUpdate only

app/control/resume_transaction.py
  one coordinator from checkpoint validation through invoke

app/control/projections.py
  isolated progress/report execution and warning aggregation

app/control/lifecycle.py
  inspect/next-action consumption of shared status; authoritative terminal work

app/storage/ledger.py
  review_applied event and singleton terminal enforcement
```

Implementation is complete only when every caller uses this shape and the old
mixed paths have been removed. Temporary adapters may exist within an
uncommitted edit while migrating tests, but no adapter, alias, feature flag, or
fallback branch is part of the delivered code.
