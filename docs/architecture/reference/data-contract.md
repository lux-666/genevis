# Run Data Contract

> **Status:** accepted; current lifecycle/evidence gaps are listed in
> [Architecture conformance](conformance.md).
> **Read when:** exact fact ownership, run paths, checkpoint budgets, crash
> behavior, replay, or retention matters.  
> **Comes from:** [Data and storage](../topics/data-and-storage.md).  
> **Feeds into:** [Execution](../topics/execution.md) and
> [Review and resume](../topics/review-and-resume.md).  
> **Related contracts:** [Resource handoff](resource-handoff.md),
> [Execution evidence](execution-evidence.md).  
> **Code evidence:** `app/storage/paths.py`, `app/storage/frozen_inputs.py`,
> `app/storage/artifacts.py`, `app/storage/ledger.py`, `app/schemas/state.py`,
> `app/graph/checkpoint_compaction.py`, `app/reports/`, and
> `app/storage/audit.py`.

**Scope:** Current workflow sessions

**Release boundary:** Current pre-release sessions use one representation.

## 1. Purpose

Gene Agent runs long-lived biological workflows with parameter recommendation,
human review, retries, large tool outputs, and durable resume. The data model
must make those workflows auditable without allowing the same mutable fact to
spread through artifacts, the artifact index, ledger events, checkpoints, and
reports.

The current data contract establishes one shared representation for all stages. The implemented
Assembly, AutoHiC, and T2T parameter-recommendation loops use the same
persistence contract rather than stage-specific audit envelopes.

This specification supersedes earlier data-layout and persistence assumptions.
Biological and stage contracts remain governed by their current specifications.

## 2. Product Decisions

1. The project is still in development. New sessions use the current
   representation only.
2. Each `runs/<session_id>/` directory is the logical ownership boundary. Its
   run-owned audit and decision evidence must not depend on another session or
   a global content-addressed object store; external inputs remain explicit
   dependencies.
3. Durable resume targets the latest safe frontier, especially a blocked human
   gate. Arbitrary historical-node time travel is not a product requirement.
4. Report filenames and artifact types are internal implementation details.
5. “Planner” in current parameter code means a stage parameter recommender. It
   does not mean method selection or parent-graph planning.
6. Public deterministic runs use `gene-agent run --manifest PATH --run-config
   PATH --data-dir PATH`. The manifest solely owns the
   session identity; `data-dir` owns layout placement at
   `runs/<session_id>/`. The run config must explicitly contain a
   `requested_stages` JSON list of `{stage, allow_local_execution}` objects.
   The list preserves literal order and repeated stage occurrences; `stage` is
   one occurrence's routing choice and `allow_local_execution` is its
   process authorization. There is no separate local-execution array.
   `requested_stages` must not be `null`; `[]` is the valid empty biological-
   stage plan. `None` is only an unfinished planner/TUI draft, not executable
   semantics, and the public structured boundary rejects it before freeze or
   execution. Freeze projects the list into the only runtime authority:
   `workflow_steps`, beginning with implicit `step-000-inventory` followed by
   position-derived IDs such as `step-001-t2t`. Runtime cannot append, reorder,
   or replace that sequence.
7. Review placement remains literal RunConfig data: Assembly and T2T do not
   create mandatory Review occurrences, and freeze never synthesizes one. Each
   explicitly requested Review step carries
   `review_target = {resource_type: "assembly", producer_step_id: "..."}` for
   the exact nearest earlier assembly-handoff occurrence it must consume.

## 3. Replay Terminology

The current contract distinguishes two forms of replay:

- **Audit replay** reconstructs the exact inputs, model request, saved model
  response, response-parse result, human decision, frozen command, execution
  outcome, and causal ordering for run-owned evidence. The run records external
  dependencies, but does not promise that those bytes are available from a
  copied run directory alone.
- **Execution replay** re-runs a command. It additionally requires tool
  environments, reference databases, and biological input bytes. The run must
  record immutable provenance and hashes for every such dependency, but shared
  environments or reference databases do not have to be copied into the run.

The accepted run-metadata vocabulary for execution-replay capability is:

- `fully_materialized`: all required input bytes are owned by the run;
- `verified_external_dependencies`: external inputs or databases are required,
  with immutable manifests and hashes recorded;
- `audit_only`: the original command can be inspected but cannot be promised to
  execute again.

The current runtime does **not** publish `control/run.json` or another single
run-level field carrying that enum. It persists the frozen input, machine
dependency, and per-attempt dependency evidence needed to assess replay, but a
consumer must inspect those records rather than trust a declared capability.
That missing run-level declaration is explicit
[conformance debt](conformance.md), not an implemented field.

“Self-contained run” guarantees audit replay while its recorded run layout is
intact. Execution replay additionally requires the frozen evidence to show that
all required bytes and environments remain available; a future run-level
capability declaration may summarize that evidence but cannot replace it.
Relocating external dependencies or absolute stage cursor paths is outside that
guarantee.

## 4. Fundamental Invariants

### 4.1 One authoritative record

Every durable fact has one authoritative owner. Other layers may copy only
bounded, deterministic lookup scalars such as IDs, status enums, timestamps,
short error codes, counts, and content hashes. Every projection must identify
or imply its authoritative source.

The rule is not “a value may appear only once.” It is “only one record may be
authoritative, and copied values must be small, immutable, and derivable.”

### 4.2 Immutable evidence

Evidence artifacts are immutable after publication. Publishing different bytes
to an existing path is an error. A changed logical document receives a new ID
and path.

### 4.3 Storage-owned references

Storage-owned immutable artifact references (`ArtifactRef.path_ref`, artifact
index rows, and refs resolved through `SessionPaths`) are POSIX paths relative
to the run root. They must not contain:

- an absolute host path;
- a `data/` prefix;
- the current session ID below the run root;
- `..` path traversal;
- a symlink that escapes the run root.

This rule is narrower than “every string ending in `_ref`”. The current runtime
also persists canonical absolute paths for declared external inputs and for
some stage-local execution cursors. Those paths are verified runtime or
dependency projections, not portable run-owned artifact references.

### 4.4 Checkpoint is a recovery cursor

Checkpoint state stores the latest control frontier and immutable references.
It is not an execution history, audit store, report cache, or parameter-attempt
database.

“Cursor” does not mean “insufficient to resume.” The checkpoint must identify
every immutable record needed to continue. Freeze and recommendation decisions
are loaded from those records; they are never recomputed from current code or
configuration during resume.

### 4.5 Ledger is a causal event stream

The ledger records what happened, in what order, and which immutable records
prove it. It does not embed candidate tables, signal arrays, asset registries,
model payloads, response-parse documents, or execution results.

Every stage-runtime fact is occurrence-bound: it carries the canonical stage,
a non-empty `workflow_step_id`, and an occurrence-aware subject or idempotency
identity. Run-level facts such as session creation, terminal outcome, and
checkpoint compaction remain explicit exceptions. The typed
`record_occurrence_event` boundary is the normal writer for domain facts, while
the central Ledger rejects a missing step ID for every runtime envelope that
carries `stage`. `session_terminal` is the explicit diagnostic exception: it
may retain a stage label without attributing the run-level outcome to one
occurrence.

Lifecycle facts belong to three separate identity domains and must never be
inferred from one another:

- a **workflow occurrence outcome** is exactly one of `stage_completed`,
  `stage_skipped`, or `stage_failed`, keyed by the frozen
  `(workflow_step_id, stage)` binding;
- a **method disposition** is exactly one of `not_started`, `skipped`,
  `completed`, or `failed`, keyed by
  `(workflow_step_id, stage, method_name)`; `skipped` and `failed` require a
  reason, while `not_started` asserts that no attempt began;
- an **attempt terminal** is exactly one of `attempt_succeeded` or
  `attempt_failed`, keyed by its immutable `attempt_id` and bound stage,
  method, and workflow occurrence.

The frozen occurrence also owns an ordered `selected_method_names` roster. It
is immutable intent, not another lifecycle outcome: registry membership and a
displayed candidate list do not select a method, while later input,
authorization, or environment checks may make a rostered method `not_started`
or `skipped`. The roster is resolved into each requested occurrence before the
RunConfig is frozen, copied into `WorkflowStep`, and is therefore stable across
repeated-stage recovery. A runtime decision that would change method identity
requires a new workflow occurrence; retries remain attempts of the same
rostered method.

These domains are orthogonal. A stage may complete after a method is skipped
when it still delivers its stage contract, may fail before a method starts,
and may complete without a process when it delivers provided or reviewed
evidence. Conversely, process success does not prove that biological
collection, resource delivery, or the stage contract succeeded.

Method disposition is not derived from attempt or occurrence terminals. A
selected method can own zero attempts, and a retrying method can own several;
the occurrence terminal records delivery rather than method choice. Freeze,
run, collect, and the shared no-attempt adapter therefore expose typed durable
cursor/evidence to a registered outcome policy only at a final method boundary.
Shared method settlement validates the resulting `MethodOutcome` against the
active occurrence roster and is the sole disposition writer. Stage modules
must not call that Ledger writer directly or infer an outcome from private
cursor text. Before any occurrence terminal is appended, the shared settlement
boundary requires exact set equality between its frozen roster and its durable
method dispositions. Missing and extra methods both fail closed; an open human
gate or retry may remain temporarily uncovered because it is not an occurrence
terminal.

The stage-to-parent `workflow_step_outcome` object is a compact checkpoint
handoff, not durable truth. It contains the exact occurrence identity,
`completed | skipped | failed`, a required reason for skipped/failed outcomes,
and any completed assembly-delivery claim. Completed/skipped outcomes advance
exactly one frozen position; failed outcomes do not advance. The shared
controller/recovery settlement boundary validates this object against the
active frozen occurrence and persists the Ledger facts. Cursor movement, a
stage-local `status`, and `route_trace` are never substitutes for this handoff.

### 4.6 Reports are projections

Reports are generated from authoritative artifacts and ledger events. Workflow
control and resume must not depend on a report.

### 4.7 Shared stage contract

Implemented executable stages use the same storage concepts: immutable
evidence, approval where required, frozen attempt, execution, review, and
retry. A stage may add typed artifact content but not a new storage layer or
ownership model.

### 4.8 Boundedness is enforced

“Keep state compact” must be enforced by schemas and tests. Comments and naming
an object `compact` are not sufficient.

### 4.9 Declared input content is logically non-empty

Every declared biological input member is validated before whole-run freeze as
a regular file with non-empty logical content. For uncompressed inputs this is
a positive file size. For gzip, bzip2, and xz inputs the validator must open the
declared compression stream and observe at least one decompressed byte;
container headers/trailers do not count. A truncated or unreadable compressed
stream is invalid. This is a lightweight content-boundary check, not a complete
FASTQ/FASTA/BAM semantic parser; stage adapters still own format-specific
validation.

This intake invariant applies to user-declared inputs. A run-owned filtering
attempt may validly produce a zero-read result and a zero-valued aggregate
statistics record. Such a method result is auditable, but it cannot be
published as a usable downstream read resource; the owning stage settles
according to its stage contract.

### 4.10 One workflow-progress reducer

Attached and detached progress use one pure occurrence reducer. Its authority
inputs are:

1. the verified frozen ordered `workflow_steps` plan;
2. canonical occurrence terminal events;
3. the canonical session terminal, including its failure scope when failed; and
4. a verified active human gate, if present.

The reducer counts occurrences by exact `workflow_step_id`, treats repeated
stage names independently, and selects the first non-terminal plan occurrence
as current only while the run is non-terminal. A completed or failed session
has no current occurrence. A blocked gate may identify the current occurrence
only when its stage and step match that first non-terminal plan position.
`stage_entered`, Ledger tail order, checkpoint cursor movement, and
`route_trace` are diagnostic inputs only and cannot choose current or increase
a terminal count.

A failed canonical `session_terminal` carries exactly one
`failure_scope: occurrence | controller_boundary`. `occurrence` requires a
contiguous `stage_completed | stage_skipped` prefix followed by exactly one
matching `stage_failed`, with no terminal after that failed occurrence.
`controller_boundary` requires no `stage_failed` and a complete contiguous
prefix containing only `stage_completed | stage_skipped`; the authoritative
finalizer may write that scope only after proving from the terminal checkpoint
that the preceding occurrence settled and the next occurrence was never
entered. A legacy or malformed failed terminal without a supported scope is
degraded rather than guessed.

The presentation value `finished` is always `completed + skipped`. Renderers do
not reinterpret a current index, a missing current occurrence, or a terminal
cursor as a count.

New writes cannot create duplicate or conflicting occurrence terminals. If an
immutable pre-invariant Ledger already contains them, the reducer selects the
conservative precedence `failed > skipped > completed`, reports explicit
integrity diagnostics, marks the projection degraded, and withholds an
authoritative current occurrence when plan-prefix integrity cannot be proven.

## 5. Fact Ownership

| Fact | Authoritative owner | Allowed elsewhere |
|---|---|---|
| Run identity and requested configuration | `control/` | IDs and refs |
| Run-owned copied input bytes, when materialized | input artifacts | Ref, hash, size, provenance summary |
| External biological input declaration and member identity | frozen input manifest plus immutable asset registry | Manifest/registry refs, ordered asset IDs, counts, and verification summary |
| Machine environment and package selection | frozen dependency manifest | Ref and verification status |
| Supplied reference/database provenance | frozen run configuration and the owning stage's immutable evidence record | Ref, bounded identity, and verification status |
| Per-file sequencing identity, platform, format, and declaration provenance | immutable asset registry | Registry ArtifactRef, ordered member IDs, counts, platform/format sets |
| Derived read-to-source correspondence | immutable read-set manifest | Manifest ArtifactRef, ordered source IDs, counts |
| Assembly primary and method-native products | immutable executed candidate bundle plus artifact index | Executed bundle ArtifactRef, selected method/candidate IDs |
| Exact profile used for a recommendation | input/profile snapshot artifact | Ref and hash |
| Descriptor, catalog, case, and policy basis | immutable run snapshot | Ref, hash, selected IDs |
| Exact model invocation | `model_request.json` | Request ref, model ID |
| Raw model result | `model_response.json` | Response ref, validity enum |
| Parsed parameter recommendation | `recommendation.json` | Recommendation ref/hash |
| Response-shape parse result | `response_parse.json` | Ref, parsed/invalid status and parser error |
| Human review request | immutable review request artifact | Request ID/ref/hash, gate ID, workflow-step ID |
| Human decision | immutable decision artifact | Decision enum, actor, ref/hash, workflow-step ID |
| Frozen runner input | immutable frozen-attempt record | Ref and attempt status |
| Executed script/command identity | frozen-attempt record plus retained `work/` script | Attempt ref and status |
| Execution result | immutable execution record | Ref, terminal status, return code |
| Selected method disposition | occurrence-bound Ledger fact, with immutable evidence refs where applicable | Stage cursor projection and report status |
| Workflow occurrence terminal outcome | one occurrence-terminal Ledger singleton | Latest compact `workflow_step_outcome` handoff and report projection |
| Attempt terminal outcome | one attempt-terminal Ledger singleton | Execution cursor projection and report status |
| stdout/stderr and tool outputs | immutable artifacts | Refs and compact counts |
| Assembly delivery by one occurrence | occurrence-bound `resource_delivered` Ledger fact referencing the exact executed-candidate bundle and primary output | Current `resource_refs["assembly"]` routing projection |
| Whole-read-set QC measurements | immutable aggregate-statistics artifact computed from the exact ordered read members | Summary artifact and compact stage projection |
| Lifecycle ordering | ledger | Last event sequence in read models |
| Artifact location and integrity metadata | artifact index | `ArtifactRef` |
| Latest resume frontier | checkpoint | Never promoted to business truth |
| Current status and session summary | `reports/` projections | Not referenced by workflow state |

An input profile is a decision snapshot derived from upstream evidence; it is
not a new claim that replaces the manifest, raw inputs, or upstream results.

## 6. Physical Layout

The tree below defines ownership roots. Child basenames are illustrative unless
a dedicated specification declares one as part of its schema.

```text
data/
├── layout.json
└── runs/
    └── <session_id>/
        ├── control/
        │   ├── tui_state.json (TUI only)
        │   ├── manifest_draft.yaml (TUI only)
        │   ├── input_manifest.yaml
        │   ├── run_config.json
        │   ├── dependency_manifest.json
        │   └── frozen_inputs.json
        ├── ledger/
        │   └── events.jsonl
        ├── checkpoints/
        │   └── checkpoints.sqlite
        ├── artifacts/
        │   ├── index.jsonl
        │   ├── inputs/
        │   ├── snapshots/
        │   └── steps/
        │       └── <workflow_step_id>/
        │           └── <stage>/
        │               ├── parameter_recommendations/
        │               ├── attempts/
        │               ├── outputs/
        │               └── reviews/<review_request_id>/
        ├── reports/
        │   ├── current_status.json
        │   ├── assembly_review.md
        │   ├── session_summary.json
        │   └── session_summary.md
        └── work/
            └── steps/<workflow_step_id>/<stage>/...
```

Rules:

- `layout.json` declares `schema_version: 2` and prevents old and new layouts
  from being mixed silently.
- Both public structured runs and confirmed TUI plans execute only from frozen
  `input_manifest.yaml` and `run_config.json`. TUI-only `manifest_draft.yaml`
  and `tui_state.json`, when present, are mutable intake records and never
  executor authority. There is no mutable `manifest.yaml` execution authority.
- `frozen_inputs.json` binds the hashes of `input_manifest.yaml`,
  `run_config.json`, and `dependency_manifest.json` and is published last.
- `control/run.json` is an accepted future metadata owner but is not currently
  created; see the replay-capability conformance entry.
- Stage directories never repeat `<session_id>`.
- Numeric recommendation and attempt directories use zero-padded,
  occurrence-local IDs such as `000`, `001`, and `002`.
- `work/` is mutable while an attempt is being prepared, then retained as part
  of that run's resumable execution workspace. Resume validates referenced
  scripts in place.
- `reports/` is outside `artifacts/` because reports are projections.
- There is no `data/artifacts/` global runtime namespace and no global object
  store in the logical reference contract.

Physical hardlinks or reflinks may reduce disk use, but a run-local artifact
reference must still resolve inside the run. Copying the run must not leave an
ArtifactRef pointing at a global object. This does not rewrite absolute paths
belonging to external-input manifests or stage-local runtime cursors.

## 7. Artifact Reference v2

Every storage-owned immutable artifact reference uses one typed contract:

```json
{
  "schema_version": 2,
  "artifact_id": "art_opaque_unique_id",
  "path_ref": "artifacts/steps/step-004-assembly/assembly/attempts/000/command.json",
  "sha256": "<sha256-of-exact-file-bytes>",
  "size_bytes": 1234,
  "artifact_type": "assembly.command"
}
```

Requirements:

- `artifact_id` is opaque and unique within the run. It must not be derived only
  from stage, method, and basename.
- `path_ref` is relative to the run root and resolves to exactly one file.
- `sha256` hashes exact file bytes, not a semantic subset with self-referential
  fields removed.
- Semantic hashes, when required, use a separate explicitly named field such as
  `proposal_semantic_hash`.
- `size_bytes` supports integrity checks and storage audit without opening the
  file.
- The artifact index may add provenance metadata, but the `ArtifactRef` remains
  sufficient to locate and verify the artifact.

## 8. Artifact Index

`artifacts/index.jsonl` is an append-only lookup index for immutable artifacts.
Each row contains the full `ArtifactRef` plus:

- workflow-step, stage, and method IDs;
- recommendation, attempt, or review ID when applicable;
- creation timestamp;
- producer event ID when available;
- compact provenance metadata;
- direct dependency refs when needed for reachability analysis.

The index does not own business status, human decisions, model reasoning, or
execution results. It must never contain two historical hashes for one mutable
path. Because artifacts are immutable, a registered path cannot change bytes.

Derived reports are not registered in the artifact index. If an externally
delivered report must itself become a retained record, export it to a new,
versioned evidence artifact with its source watermark and content hash.

## 9. Run Snapshots

Run snapshots preserve the exact basis used by parameter recommenders:

- method descriptor and capability;
- parameter catalog;
- selected literature/evidence cases;
- knowledge-pack metadata;
- retry limits and approval policy;
- relevant tool and reference-data provenance.

Retries derive from the initial immutable recommendation chain and must not
silently reload a changed catalog, policy, or evidence set. A stage may either
share the initial basis ref or publish an attempt-scoped copy derived from that
verified basis: Assembly currently shares its initial ref, while AutoHiC writes
a basis artifact for each recommendation ID after loading retry inputs from the
initial chain. Attempt-scoped copies must remain content-consistent with the
initial immutable basis except for explicitly modeled retry context.

Cross-run physical deduplication is an implementation detail only. Runtime code
must not replace a session-relative snapshot ref with a global hash-only ref.

## 10. Parameter-Recommendation Data Boundary

[`parameter-recommendation.md`](parameter-recommendation.md) is normative for the
model DTO, prompt rendering, response parsing, approval, and stage/model
responsibility split. Data v2 owns only the durable placement of those facts.

For a method that uses parameter recommendation:

- the exact model request is durable before invocation;
- the raw response and parsed recommendation are different immutable facts;
- the review request binds the immutable recommendation by ref and hash;
- rejection or parse failure creates no execution attempt;
- a retry binds the preceding sealed attempt and does not silently reload a new
  evidence or policy basis.

A recommendation manifest groups the refs required to verify those facts.
Checkpoint state carries its bounded cursor/ref, never the request, response,
profile, basis, or recommendation bodies. Stage-specific DTOs and storage
adapters are expected; a second fact-ownership model is not.

## 11. Execution Attempt Data Boundary

Freezing is completed before execution. If recommendation or approval applies,
it must finish first; methods without those gates freeze directly from their
validated deterministic inputs.

The universal durable minimum is deliberately small:

1. a frozen-attempt record that identifies the exact runner input;
2. the canonical execution record defined by
   [`execution-evidence.md`](execution-evidence.md);
3. immutable refs for collected business outputs when they are published.

The frozen-attempt record contains the complete normalized runner input needed
for execution, including its `script_path`, `working_dir`, expected output
paths, environment projection, Conda selection, and typed input dependencies,
subject to the secrets policy. Registry-owned dependencies retain asset
identity plus size/SHA-256 evidence; run-owned dependencies retain a complete
`ArtifactRef` and the exact path supplied to the command. Explicit external
files retain size/SHA-256 evidence, and external directories retain a
deterministic tree hash plus file-count and total-byte evidence. The record is
persisted before the runner starts, and all dependencies are revalidated
immediately before execution.

For a parameterized method execution that originates in the model
recommendation/approval lifecycle, `ParameterAttempt` is the sole checkpoint
relation owner for `frozen_parameters_ref`, `command_ref`,
`frozen_attempt_ref`, `execution_ref`, and `review_ref`. The enclosing stage
cursor stores the ordered attempt chain, `current_parameter_attempt_id`,
lifecycle status, and only bounded routing projections. It must not duplicate
those refs in method-specific keys such as `*_frozen_attempt_ref` or
`*_execution_result`. A retry appends a new `ParameterAttempt` and advances the
current ID; it never overwrites the refs on the preceding attempt. The sealed
attempt manifest and terminal timeline include the same per-attempt refs,
including the exact `frozen_attempt_ref` consumed by the runner.

For a non-parameterized method, its typed stage cursor may own the frozen-attempt
and execution-record refs directly. In both cases resume loads the frozen record
and does not regenerate a command from current descriptors, catalogs,
configuration, or environment.

The referenced script remains under `work/`; the run retains that workspace so
the same frozen input can be resumed without regenerating a command. Resume
validates that the script still exists. If an operator manually deletes the
referenced work file, that attempt is no longer resumable and recovery fails
closed. Data v2 does not copy or hash the script solely to survive such manual
deletion, and it never reconstructs the command from current configuration.

A sealed attempt manifest is required only when a retry/review workflow needs
to bind several independently published records into an immutable chain. A
simple one-shot method does not need a ceremonial
`frozen_parameters.json + command.json + run.sh + attempt_manifest.json` bundle.
Basenames are implementation details; schemas, artifact types, hashes, and refs
form the contract.

An attempt terminal describes only that frozen invocation. The Ledger append
boundary, while holding the same exclusive lock used for sequence allocation,
permits at most one of `attempt_succeeded` and `attempt_failed` for an immutable
attempt ID. Exact semantic replay returns the existing row. A different
terminal type, stage/method/occurrence binding, summary, or ref set is a
`LedgerConflictError`, even when a caller supplies another idempotency key.

Method disposition is a separate occurrence fact. It covers selected methods
that never own an attempt as well as methods that do:

| Disposition | Meaning | Attempt relation |
|---|---|---|
| `not_started` | Preconditions failed before execution was eligible | no attempt-start or terminal event |
| `skipped` | The stage deliberately did not invoke a selected executable method | no attempt-start or terminal event; reason required |
| `completed` | Required method work and collection completed | at least one matching successful attempt when executable; a selected planner-only pass-through completes without an attempt |
| `failed` | Started method work or collection failed | matching failed attempt when one was frozen; reason required |

One method disposition cannot settle a workflow occurrence by itself. The
stage contract decides whether the occurrence still delivered, explicitly
skipped, or failed.

`MethodOutcome` is an internal settlement value, not another checkpoint
authority. The shared lifecycle/no-attempt node derives it through an explicit
registered policy from already durable typed cursor/evidence and settles it
before parent routing continues. A retryable attempt failure emits only an
attempt fact until policy chooses a final method outcome.

## 12. Review Contract

Every human gate creates an immutable request under:

```text
artifacts/steps/<workflow_step_id>/<stage>/reviews/<review_request_id>/request.json
artifacts/steps/<workflow_step_id>/<stage>/reviews/<review_request_id>/decision.json
```

Supporting evidence packets and visual attachments are immutable artifacts
referenced by `request.json`. A candidate comparison is a section of the review
packet, not a second near-identical Markdown report.

Checkpoint `active_interrupt` contains only bounded control data:

```json
{
  "stage": "assembly",
  "gate_id": "parameter_approval",
  "review_request_id": "review_...",
  "request_ref": {"...": "ArtifactRefV2"},
  "request_hash": "...",
  "blocked_at": "..."
}
```

Allowed decisions, candidates, signals, metrics, recommendation rationale, and
review policy live in the request artifact and are loaded through its verified
ref. Resume validates request identity, request hash, decision schema, and any
bound recommendation/attempt hash before applying the decision.

`payload.max_blocked_seconds` is the effective frozen RunConfig review-policy
value: the default is `300`, and a per-stage override is valid only for a stage
registered in the review registry. It records foreground behavior, not a
durable deadline. Every new run verifies stdin/stdout TTY capability before
freeze and enters the same attached executor. `-1` means indefinitely wait;
`0` immediately triggers the gate's registered eligible resolver; and a
positive value means an Enter-claim countdown that triggers the same resolver
at expiry.

The timer starts only after the immutable request exists. For
`parameter_approval`, timeout resolution means approving the exact persisted
recommendation bound by the request; it does not generate, edit, merge, or
replace parameter values. Resolver eligibility, immutable request and
recommendation bindings, rationale artifacts, and the registered typed
intent/application lifecycle remain authoritative. A countdown-triggered automatic decision additionally
requires the internal resume transaction source and bound countdown metadata to
identify the current `foreground_countdown`; its decision origin remains
resolver-owned (`llm_provisional` or `auto_rule`), and request metadata cannot
authorize itself.

Ctrl-C, EOF, or process exit leaves the checkpoint `blocked_human`; no
background/daemon timer is persisted or started. Re-executing the same frozen
session does not invoke an already-blocked checkpoint or restore the timer.
Later manual review/resume must not infer elapsed time from `blocked_at` and has
no public timeout override or strict/auto mode.

The review registry is the sole declaration and typed dispatch point for gate
IDs, allowed decisions, occurrence validation, timing metadata, and each
gate's prepare/apply contract. It does not own stage payload meaning or prompt
branches: registered stage preparation owns durable domain effects, and
registered verified application projects the typed effect into checkpoint state.

## 13. Ledger v2

Each `ledger/events.jsonl` row contains a common envelope:

```json
{
  "schema_version": 2,
  "sequence": 42,
  "event_id": "evt_...",
  "idempotency_key": "...",
  "session_id": "...",
  "timestamp": "...",
  "event_type": "attempt_succeeded",
  "workflow_step_id": "step-004-assembly",
  "stage": "assembly",
  "subject_id": "attempt_000",
  "refs": [],
  "summary": {}
}
```

Requirements:

- `sequence` is monotonically increasing within the session.
- `event_id` is unique and `idempotency_key` prevents duplicate effects after
  node replay or process restart.
- `refs` point to authoritative artifacts.
- `summary` contains only bounded query fields.
- Review events bind by workflow-step ID and review request ID, never only by
  stage name.
- Each frozen occurrence has at most one terminal event among
  `stage_completed`, `stage_skipped`, and `stage_failed`. The append boundary
  keys uniqueness by workflow-step ID and validates the stage binding. Exact
  semantic replay returns the existing row; another outcome or changed
  summary/refs conflicts regardless of idempotency key. The same append lock
  also proves that the occurrence's durable disposition names equal its frozen
  `selected_method_names` roster before allowing any terminal.
- Each immutable attempt has at most one terminal event among
  `attempt_succeeded` and `attempt_failed`, under the same append-lock rule.
- `method_disposition_recorded` is occurrence-bound and keyed by method name.
  It records only `not_started | skipped | completed | failed`, a bounded
  reason when required, and refs to its authoritative evidence.
- `resource_delivered` is occurrence-bound. For the current v1 assembly
  delivery contract it names `resource_type="assembly"`, records
  `delivery_mode="materialized" | "reused"`, and carries exact
  `candidate_bundle_ref` and `primary_output_ref` roles. It is written before
  that occurrence's `stage_completed` event.
- `env_preflight_blocked`, `candidate_comparison_generated`,
  `registered_assembly_candidate_bound`, and `automatic_review_failed` are
  stage-runtime facts and require the exact producing occurrence. Their
  idempotency identities include that occurrence, so two same-stage failures
  cannot collapse into one event.
- Route facts use `stage_entered`, the three occurrence terminal events, and
  `session_terminal`.
  A `stage_skipped` event is valid only for an explicit stage-local decision;
  absent frozen occurrences never produce one.
- A failed `session_terminal` records `failure_scope=occurrence` only with the
  matching failed occurrence, or `failure_scope=controller_boundary` only after
  the finalizer proves a fully settled prefix and an unentered next occurrence.
  Completed terminals have no failure scope.
- Artifact metadata is not copied into ledger events. Attempt or review events
  carry only the artifact refs required to explain the causal transition.

Every append path, including session terminals, enforces two independent
invariants while holding the same exclusive append lock. It first validates the
event domain's natural-key uniqueness and remembers an exact domain replay
without returning. It then validates that the supplied idempotency key owns at
most one semantic fact and that any existing owner has the same normalized
semantic body. Only after both checks succeed may it return an exact domain/key
replay; otherwise it appends one new row. A natural-key replay never hides reuse
of that call's idempotency key by another fact.

The full ordered route, explicit method dispositions and stage-local skip
reasons, immutable assembly deliveries, review lifecycle, and terminal outcome
must be reconstructible without checkpoint or reports.

Runtime progress has no durable projection of its own. Both an attached
terminal and detached inspection call the reducer defined in section 4.10.
Before attached rendering, the parent has already committed the corresponding
delivery and occurrence-terminal Ledger events through shared settlement;
controller state may supply a candidate active gate but cannot supply terminal
counts. Detached inspection derives the same inputs from verified frozen
control data, Ledger/review facts, and the canonical session terminal. SQLite
availability, progress-marker state, and the execution-lock observation remain
adjacent operational signals, not
workflow-status authority. Counts and percentages are ephemeral presentation
arithmetic and must not be persisted as artifacts, reports, Ledger events,
checkpoint fields, progress tables, or batch state.

Both attached and detached text renderers print `finished` directly from
`completed_steps + skipped_steps`. A renderer warns about degraded history only
when `projection_authority == "degraded_stage_terminal_projection"`; the
non-empty string `"authoritative"` is not a warning condition. When current is
withheld, the renderer prints an unknown marker rather than inventing `done` or
turning a cursor position into progress.

Machine-readable detached inspection publishes `inspect_schema_version: 1`.
Its `workflow_progress` object uses only a verified frozen RunConfig for the
ordered denominator and occurrence-qualified `stage_completed`,
`stage_skipped`, and `stage_failed` Ledger events for terminal counts. Repeated
stage names remain separate by `workflow_step_id`; failed and blocked steps are
reported separately and are not counted as finished. If frozen inputs are
missing or invalid, `plan_source` is `unavailable` and all plan-derived fields
are `null` rather than inferred from registry order or observed events.
Detached inspection also withholds the current occurrence, `active_interrupt`,
`decision_requirements`, and any actionable review `next_action` until the
frozen workflow inputs verify; raw Ledger events remain visible for diagnosis
but cannot authorize a resume.

Legacy duplicate/conflicting stage or attempt terminals are never resolved by
"last event wins". Their conservative read model emits integrity diagnostics;
workflow progress is marked degraded, and method failure lists contain only the
canonical attempt projection so one method cannot be reported as both succeeded
and failed.

The adjacent `execution` object reports `frozen_inputs_status`, read-only
durable progress as `fresh`, `checkpoint`, `partial`, or `unavailable`, and the
local advisory execution-lock observation as `true`, `false`, or `null` when
unknown. The lock does not prove remote liveness or scheduler state, and
inspection must not create the control directory or lock file.

## 14. Checkpoint

Checkpoint state may contain:

- session and thread IDs;
- immutable `workflow_steps`, active occurrence index, current stage, and
  supervision state;
- the latest compact `workflow_step_outcome` handoff awaiting or reflecting
  shared occurrence settlement; it is replaceable recovery state, not terminal
  authority;
- immutable stage-scoped seeds; each repeated occurrence receives a fresh copy
  of its stage seed rather than an occurrence-specific method configuration;
- current stage status cursor;
- current parameter-recommendation ref;
- current parameter-attempt ID and compact ordered `ParameterAttempt` chain, or
  direct attempt refs for a non-parameterized method;
- compact active-interrupt ref;
- current retry counters;
- control/config refs;
- bounded stage-owned review projections needed for routing or display, such as
  Assembly Review candidate, signal, and metric summaries;
- a bounded diagnostic route tail.

The compact settlement handoff has this shape:

```json
{
  "workflow_step_id": "step-004-assembly_dedup",
  "stage": "assembly_dedup",
  "outcome": "completed",
  "reason": null,
  "deliveries": [
    {
      "resource_type": "assembly",
      "delivery_mode": "reused",
      "candidate_bundle_ref": {"schema_version": 2, "artifact_id": "..."},
      "primary_output_ref": {"schema_version": 2, "artifact_id": "..."}
    }
  ]
}
```

`reason` is absent for ordinary completion and required for `skipped` or
`failed`. `deliveries` is absent unless the occurrence completed, contains at
most one v1 assembly delivery, and uses complete `ArtifactRef` objects. The
parent rejects an outcome for any occurrence other than the active or
just-settled frozen step. This object may be replaced by the next occurrence's
handoff after its Ledger terminal exists.

Checkpoint state must not contain:

- parameter catalogs, rules, cases, or descriptor snapshots;
- full input profiles or model contexts;
- model responses or reasoning;
- full or unbounded candidate, signal, metric, or comparison-table bodies;
- response-parse documents;
- argv, script bodies, environment maps, or execution results;
- stdout/stderr tails;
- report payloads;
- unbounded route traces or review-note lists.

The central checkpoint boundary treats `stage_outputs` as a bounded JSON map
keyed by known stages. Current stage packages also provide typed cursor
adapters and most writes go through those adapters, but the central boundary
does not yet enforce one universal per-stage schema or reject every unknown
cursor field. It does enforce JSON shape, known stage names, per-cursor and
aggregate byte limits, and artifact-body/frozen-attempt embedding guards.
The map represents only the active/latest cursor for each stage name. Entering
a repeated occurrence replaces that stage slot from immutable `stage_seeds`;
it must never be used as cross-occurrence history. Historical identity and
evidence are keyed by `workflow_step_id` in artifacts, reviews, attempts, and
ledger events.
Bounded stage-owned review summaries are routing projections, not durable
business truth; their owning evidence remains in indexed immutable artifacts.

Initial budgets:

- target canonical serialized `TopLevelState`: at most 64 KiB;
- hard test ceiling: 256 KiB;
- active interrupt: at most 4 KiB;
- any route tail: at most 16 entries.

A budget increase requires an architecture change with measured evidence. It
must not be used to accommodate an embedded artifact.

While a run is active or blocked, retain the latest resumable checkpoint.
Intermediate historical checkpoints are not a supported API. After a terminal
event is committed and ledger reconstruction plus checkpoint-free artifact
reachability pass, the database is compacted to one standalone terminal
checkpoint.

## 15. Reports v2

Reports are read models:

- `current_status.json` is atomically overwritten with the latest operator
  status and action;
- `assembly_review.md` is a derived assembly-review view currently generated
  from live stage state and its indexed review packet;
- `session_summary.json` is the structured session outcome and timeline;
- `session_summary.md` is an optional rendering of the same summary model.

The summary model may contain assembly review, candidate comparison, human
decisions, failures, gaps, and artifact links as sections. Separate files must
exist only when they represent a different immutable evidence record or a
genuinely different audience contract.

The report subsystem's `current_status` and session-summary writers record a
source watermark containing at least the last ledger sequence and
artifact-index digest. Reports are not ledger events, artifact-index rows, or
resume inputs. The accepted contract is that every report can be regenerated
and carries an equivalent watermark; current-status and the stage-owned
Assembly Review Markdown do not yet meet the full reconstruction/watermark
boundary, as listed in [Architecture conformance](conformance.md).

Trace reconstruction validates the complete frozen-input transaction and uses
only the resulting verified RunConfig to project `workflow_steps`. If marker,
hash, schema, or biological validation fails, it does not parse the changed
RunConfig directly: it falls back to occurrence-bearing Ledger and artifact
facts, reporting gaps when those durable facts are insufficient rather than
inventing a registry order.

## 16. Central Path Ownership

Only the storage layer constructs run paths. The current `SessionPaths` surface
used directly or through stage/storage helpers is:

```python
paths.workflow_step_artifacts_dir(workflow_step_id, "assembly")
paths.workflow_step_artifact_path(workflow_step_id, "assembly", "parameter_recommendations", "000", "...")
paths.workflow_step_artifact_path(workflow_step_id, "assembly", "attempts", "000", "...")
paths.workflow_step_artifact_path(workflow_step_id, "assembly", "reviews", review_request_id, "request.json")
paths.workflow_step_work_dir(workflow_step_id, "assembly")
paths.workflow_step_work_path(workflow_step_id, "assembly", "attempts", "000", "...")
paths.report_path("session_summary.json")
```

No stage may append `session_id` below `run_dir` or construct a
`data/artifacts` path. The current contract has one write layout.

An architecture test should reject direct stage-level construction of
`artifacts`, `work`, `ledger`, `checkpoints`, or `reports` roots outside the
storage API.

## 17. Durable Write and Crash Protocol

For one evidence publication:

1. Serialize and validate new evidence entirely in memory.
2. Write a temporary file inside the destination directory.
3. Flush, verify, and atomically rename it to its immutable path.
4. Append its artifact-index row idempotently.
5. Append the semantic ledger event idempotently.
6. Return the compact state update for checkpointing.

Occurrence settlement uses one stricter shared order. The parent controller and
terminal recovery both invoke the same settlement boundary:

1. The stage publishes and indexes every immutable result required by its
   contract.
2. The stage returns a validated `workflow_step_outcome`; it does not write its
   own occurrence terminal.
3. The settlement boundary validates the outcome against the active frozen
   occurrence and, for an assembly delivery, checks that the current
   `ResourceRef` projection names the same candidate bundle and primary output.
4. The boundary appends or exactly replays each `resource_delivered` fact.
5. The boundary appends or exactly replays the one occurrence terminal fact.
6. Only after those Ledger facts exist may attached progress render the
   transition or the next occurrence be presented as current.

A crash after `resource_delivered` but before `stage_completed` leaves an
unfinished occurrence with a recoverable delivery prefix. Replay must verify
the exact delivery and append the missing terminal; it must not create another
delivery or infer completion from the advanced checkpoint cursor.

Crash rules:

- An artifact/index row without a semantic event is an orphan candidate, not a
  completed transition.
- A semantic event with a stale checkpoint remains authoritative audit truth.
  Current durable resume still requires the retained checkpoint, except for
  narrowly implemented stage/evidence replay recovery; there is no generalized
  Ledger-to-checkpoint reconstruction path.
- Repeating a completed publish with the same path and hash succeeds
  idempotently.
- Repeating it with different bytes fails closed.
- Checkpoint must never be the only owner of a completed fact.
- A delivery without its following completed occurrence is not consumable by a
  downstream Review, even when the mutable current `ResourceRef` already points
  at the delivered bytes.

## 18. Secrets and Sensitive Data

Durable records must exclude:

- API keys and authorization headers;
- passwords, tokens, and private keys;
- secret environment variables;
- credentials embedded in URLs;
- unrestricted full environment dumps.

Model and command records use an allowlisted configuration projection. When a
secret influences execution, record its presence and source name, not its
value.

## 19. Retention and Garbage Collection

GC is reachability-based, never filename- or age-only deletion.

Retention roots include:

- control manifests and configuration refs;
- the artifact refs reachable from the ledger, artifact index, and retained
  checkpoint;
- active review requests and decisions;
- sealed attempt manifests and terminal timelines;
- retained externally delivered evidence exports.

Deletion policy:

- `work/` is outside automatic cleanup and GC. An operator may remove it only
  by explicitly accepting that affected attempts can no longer resume.
- A report may be removed only when an implemented durable-record-only path
  can regenerate that report with an equivalent source watermark. This is
  currently true for session summaries, not for every file under `reports/`.
- Intermediate checkpoints may be pruned according to section 14.
- Immutable artifacts may be deleted only when the session is terminal and
  they are unreachable from every retention root.
- GC must first produce a dry-run manifest containing each candidate's
  run-relative path, hash, size, and reason.
- Immediately before deletion, an executor must revalidate terminal state and
  every candidate path, hash, size, reachability result, and layer
  classification; any mismatch fails closed.
- Deletion requires explicit operator confirmation. The executor must reject
  path traversal, absolute paths, symlink escapes, and every `work/` entry.
- Artifact-index compaction happens only after all approved artifact deletions
  succeed.

Run-owned ArtifactRefs are session-local, so deleting another run or a global
cache cannot break those references. External-input manifests and stage-local
absolute paths remain explicit dependencies and may still make execution replay
unavailable after relocation or source removal.

## 20. Current Representation

Each pre-release session has one representation:

- no dual write;
- no fallback path or hash-only payload/profile runtime branch;
- no runtime migration or alternate-layout lookup;
- no aliases for report filenames or artifact types;
- runtime code does not mix layouts within one run root.

There is no checkpoint migration for this pre-release representation. A code
upgrade can make an older checkpoint unreadable; complete active sessions before
upgrading or retain the matching older checkout to resume them.

Reasonable dual representations remain allowed: raw output plus normalized
summary, immutable artifact plus metadata-only index, compact ledger event plus
artifact ref, and JSON report plus regenerable Markdown.

## 21. Current Conformance

The current implementation enforces the following:

- A new run contains no repeated session ID below its run root.
- Storage-owned ArtifactRefs are session-relative and hash-verifiable; external
  input manifests and some stage cursors retain canonical absolute paths.
- Inventory emits a typed asset-registry `ArtifactRef`; new checkpoints and
  resource metadata do not use an absolute registry path or embedded asset
  records.
- Sequencing files retain per-member identity and platform provenance even
  when several manifest entries share one `asset_type`.
- Assembly publishes one assembler-origin backbone. Planned bundles contain no
  output refs; executed bundles reference their plan, primary FASTA, and
  adapter-declared native outputs.
- Copying a run preserves run-owned audit artifacts, but is not a universal
  portability guarantee for external dependencies or absolute cursor paths.
- A blocked review resumes from the latest frontier using the checkpoint plus
  verified run-local refs and any declared external dependencies required by
  the stage.
- Terminal compaction preserves reconstruction of frozen occurrence order,
  explicit stage-local skip reasons, decisions, failures, and terminal outcome
  while retaining one standalone checkpoint.
- Corrupting a referenced artifact causes replay/resume to fail closed.
- Replaying a node does not duplicate ledger effects or artifact-index rows.
- Recommendation rejection creates no execution attempt and consumes no retry
  budget.
- Assembly and Hi-C parameter recommenders share persistence and artifact
  cursors without sharing one biological/model DTO.
- Checkpoint size budgets are enforced in tests.
- Deleting reports does not change audit facts. Session summaries can be
  regenerated from the Ledger and artifact index; current-status and Assembly
  Review reconstruction limitations are listed in conformance debt.
- The storage-audit command reports bytes by layer, largest checkpoint fields,
  duplicate content, unreachable artifacts, retained work bytes, and
  reclaimable report data.

The deletion portion of section 19 is not implemented. `storage-audit` is
read-only and has no `--apply` option. Its optional cleanup manifest is a
dry-run projection: it currently labels every file under `reports/` as a
regenerable deletion candidate, even though only session summaries meet the
durable-record-only regeneration boundary; `work/` is reported but explicitly
retained. There is no filesystem deletion executor for unreachable immutable
artifacts and no post-GC artifact-index compaction. These gaps are conformance
debt, not a reduction of the current data contract.
Genome Survey k-mer handoffs are typed `ArtifactRef` values in the checkpoint
cursor and in frozen method dependencies; plain path strings are rejected at
the stage boundary.
