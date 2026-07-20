# Data Architecture v2

**Status:** Accepted

**Accepted:** 2026-07-15

**Implementation:** Partial. The v2 run layout, immutable refs, ledger/report
ownership, recommendation persistence, typed bounded stage cursors, universal
frozen-attempt refs, process-result evidence, read-only storage audit, and a
dry-run cleanup manifest are in use. A code audit on 2026-07-20 confirmed that
some stage producers still write directly to fixed `artifacts/` paths before
registration; whole-checkpoint budgets, terminal compaction, deletion-performing
artifact GC, and a unified `storage-audit` CLI remain incomplete.

**Scope:** New workflow sessions created after the v2 cutover

**Compatibility:** No legacy-session read, resume, migration, or dual-write contract

## 1. Purpose

Gene Agent runs long-lived biological workflows with parameter recommendation,
human review, retries, large tool outputs, and durable resume. The data model
must make those workflows auditable without allowing the same mutable fact to
spread through artifacts, the artifact index, ledger events, checkpoints, and
reports.

Data v2 establishes one shared contract for all stages. Assembly and AutoHiC
have parameter-recommendation loops; a future T2T parameter recommender must
reuse the same contract rather than introducing
stage-specific audit envelopes.

This specification supersedes data-layout and persistence assumptions in phase
documents. Biological and stage contracts remain governed by their existing
documents.

## 2. Accepted Product Decisions

1. The project is still in development. Data v2 does not preserve compatibility
   with old sessions.
2. Each `runs/<session_id>/` directory is the logical ownership boundary. Its
   audit and decision replay must not depend on another session or a global
   content-addressed object store.
3. Durable resume targets the latest safe frontier, especially a blocked human
   gate. Arbitrary historical-node time travel is not a product requirement.
4. Existing report filenames and artifact types are not external compatibility
   contracts.
5. “Planner” in the current roadmap means a stage parameter recommender. It does
   not mean method selection or parent-graph planning.

## 3. Replay Terminology

Data v2 distinguishes two forms of replay:

- **Audit replay** reconstructs the exact inputs, model request, saved model
  response, response-parse result, human decision, frozen command, execution outcome, and
  causal ordering. Audit replay is mandatory from the run directory alone.
- **Execution replay** re-runs a command. It additionally requires tool
  environments, reference databases, and biological input bytes. The run must
  record immutable provenance and hashes for every such dependency, but shared
  environments or reference databases do not have to be copied into the run.

`control/run.json` must declare the execution-replay capability honestly:

- `fully_materialized`: all required input bytes are owned by the run;
- `verified_external_dependencies`: external inputs or databases are required,
  with immutable manifests and hashes recorded;
- `audit_only`: the original command can be inspected but cannot be promised to
  execute again.

“Self-contained run” always guarantees audit replay. It guarantees execution
replay only when the declared capability says so.

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

### 4.3 Session-relative references

Every durable internal reference is a POSIX path relative to the run root. It
must not contain:

- an absolute host path;
- a `data/` prefix;
- the current session ID below the run root;
- `..` path traversal;
- a symlink that escapes the run root.

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

### 4.6 Reports are projections

Reports are generated from authoritative artifacts and ledger events. Workflow
control and resume must not depend on a report.

### 4.7 Shared stage contract

Assembly, Hi-C, T2T, and later executable stages use the same storage concepts:
parameter recommendation, response parsing, approval, frozen attempt, execution,
review, and retry. A stage may add typed artifact content but not a new storage
layer or ownership model.

### 4.8 Boundedness is enforced

“Keep state compact” must be enforced by schemas and tests. Comments and naming
an object `compact` are not sufficient.

## 5. Fact Ownership

| Fact | Authoritative owner | Allowed elsewhere |
|---|---|---|
| Run identity and requested configuration | `control/` | IDs and refs |
| Imported input bytes | input artifacts | Ref, hash, size, provenance summary |
| External input/database provenance | immutable dependency manifest | Ref and verification status |
| Exact profile used for a recommendation | input/profile snapshot artifact | Ref and hash |
| Descriptor, catalog, case, and policy basis | immutable run snapshot | Ref, hash, selected IDs |
| Exact model invocation | `model_request.json` | Request ref, model ID |
| Raw model result | `model_response.json` | Response ref, validity enum |
| Parsed parameter recommendation | `recommendation.json` | Recommendation ref/hash |
| Response-shape parse result | `response_parse.json` | Ref, parsed/invalid status and parser error |
| Human review request | immutable review request artifact | Request ID/ref/hash, gate ID |
| Human decision | immutable decision artifact | Decision enum, actor, ref/hash |
| Frozen runner input | immutable frozen-attempt record | Ref and attempt status |
| Executed script/command identity | frozen-attempt record plus retained `work/` script | Attempt ref and status |
| Execution result | immutable execution record | Ref, terminal status, return code |
| stdout/stderr and tool outputs | immutable artifacts | Refs and compact counts |
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
        │   ├── run.json
        │   ├── manifest.yaml
        │   ├── run_config.json
        │   └── dependency_manifest.json
        ├── ledger/
        │   └── events.jsonl
        ├── checkpoints/
        │   └── checkpoints.sqlite
        ├── artifacts/
        │   ├── index.jsonl
        │   ├── inputs/
        │   ├── snapshots/
        │   ├── inventory/
        │   ├── read_qc/
        │   ├── genome_survey/
        │   ├── assembly/
        │   │   ├── parameter_recommendations/
        │   │   │   └── 000/
        │   │   ├── attempts/
        │   │   │   └── 000/
        │   │   └── outputs/
        │   ├── assembly_review/
        │   ├── hic_scaffolding/
        │   │   ├── parameter_recommendations/
        │   │   ├── attempts/
        │   │   └── outputs/
        │   ├── t2t/
        │   │   ├── parameter_recommendations/
        │   │   ├── attempts/
        │   │   └── outputs/
        │   └── reviews/
        │       └── <review_request_id>/
        ├── reports/
        │   ├── current_status.json
        │   ├── assembly_review.md
        │   ├── session_summary.json
        │   └── session_summary.md
        └── work/
            └── <stage>/...
```

Rules:

- `layout.json` declares `schema_version: 2` and prevents old and new layouts
  from being mixed silently.
- Stage directories never repeat `<session_id>`.
- Numeric recommendation and attempt directories use zero-padded, stage-local
  IDs such as `000`, `001`, and `002`.
- `work/` is mutable while an attempt is being prepared, then retained as part
  of that run's resumable execution workspace. Built-in cleanup and GC do not
  delete it.
- `reports/` is outside `artifacts/` because reports are projections.
- There is no `data/artifacts/` global runtime namespace and no global object
  store in the logical reference contract.

Physical hardlinks or reflinks may reduce disk use, but a run-local reference
must still resolve inside the run. Copying the run must not leave a logical ref
pointing at a global object.

## 7. Artifact Reference v2

Every durable reference uses one typed contract:

```json
{
  "schema_version": 2,
  "artifact_id": "art_opaque_unique_id",
  "path_ref": "artifacts/assembly/attempts/000/command.json",
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

- stage and method IDs;
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

Identical knowledge is snapshotted once per run and shared by run-local refs.
Retries reference the initial snapshot and must not silently reload a changed
catalog, policy, or evidence set.

Cross-run physical deduplication is an implementation detail only. Runtime code
must not replace a session-relative snapshot ref with a global hash-only ref.

## 10. Parameter-Recommendation Data Boundary

[`parameter-recommenders.md`](parameter-recommenders.md) is normative for the
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
paths, environment projection, and Conda selection, subject to the secrets
policy. It is persisted before the runner starts.

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

An operator-directed Assembly rerun at the review gate has a different
parameter source and is outside that `ParameterAttempt` ownership rule. Its
durable relation is `ReviewDecision -> FrozenAttempt -> ExecutionResult ->
CandidateBundle`: the review decision owns the operator-supplied parameters,
and the candidate bundle binds the exact frozen and execution refs. This path
must not invoke the model again or synthesize recommendation/approval evidence
solely to imitate the recommendation lifecycle.

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

## 12. Review Contract

Every human gate creates an immutable request under:

```text
artifacts/reviews/<review_request_id>/request.json
artifacts/reviews/<review_request_id>/decision.json
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
- Review events bind by review request ID, never only by stage name.
- Route and skip facts use explicit events such as `stage_entered`,
  `stage_skipped`, `stage_completed`, and `session_terminal`.
- Artifact metadata is not copied into ledger events. Attempt or review events
  carry only the artifact refs required to explain the causal transition.

The full ordered route, skip reasons, review lifecycle, and terminal outcome
must be reconstructible without checkpoint or reports.

## 14. Checkpoint v2

Checkpoint state may contain:

- session and thread IDs;
- current stage and supervision state;
- current stage status cursor;
- current parameter-recommendation ref;
- current parameter-attempt ID and compact ordered `ParameterAttempt` chain, or
  direct attempt refs for a non-parameterized method;
- compact active-interrupt ref;
- current retry counters;
- control/config refs;
- a bounded diagnostic route tail.

Checkpoint state must not contain:

- parameter catalogs, rules, cases, or descriptor snapshots;
- full input profiles or model contexts;
- model responses or reasoning;
- candidates, signals, metrics, or comparison tables;
- response-parse documents;
- argv, script bodies, environment maps, or execution results;
- stdout/stderr tails;
- report payloads;
- unbounded route traces or review-note lists.

`stage_outputs` becomes a typed map of compact stage cursors. It is not
`dict[str, Any]` and must reject unknown large fields at the checkpoint
boundary.

Initial budgets:

- target canonical serialized `TopLevelState`: at most 64 KiB;
- hard test ceiling: 256 KiB;
- active interrupt: at most 4 KiB;
- any route tail: at most 16 entries.

A budget increase requires an architecture change with measured evidence. It
must not be used to accommodate an embedded artifact.

While a run is active or blocked, retain the latest resumable checkpoint.
Intermediate historical checkpoints are not a supported API. After a terminal
event is committed and audit reconstruction passes, the database may be
compacted to one terminal checkpoint or removed by retention policy.

## 15. Reports v2

Reports are read models:

- `current_status.json` is atomically overwritten with the latest operator
  status and action;
- `assembly_review.md` is a derived assembly-review view generated from
  authoritative review, ledger, and artifact records;
- `session_summary.json` is the structured session outcome and timeline;
- `session_summary.md` is an optional rendering of the same summary model.

The summary model may contain assembly review, candidate comparison, human
decisions, failures, gaps, and artifact links as sections. Separate files must
exist only when they represent a different immutable evidence record or a
genuinely different audience contract.

Every generated report records a source watermark containing at least the last
ledger sequence and artifact-index digest. Reports may be deleted and
regenerated. They are not ledger events, artifact-index rows, or resume inputs.

## 16. Central Path Ownership

Only the storage layer constructs run paths. Stage code requests paths through
a shared API, conceptually:

```python
paths.stage_artifacts("assembly")
paths.parameter_recommendation("assembly", 0)
paths.attempt("assembly", 0)
paths.review(review_request_id)
paths.stage_work("assembly", 0)
paths.reports()
```

No stage may append `session_id` below `run_dir`, construct a `data/artifacts`
path, or decide whether a path is legacy. Data v2 has one write layout and no
legacy branch.

An architecture test should reject direct stage-level construction of
`artifacts`, `work`, `ledger`, `checkpoints`, or `reports` roots outside the
storage API.

## 17. Durable Write and Crash Protocol

For one logical transition:

1. Serialize and validate new evidence entirely in memory.
2. Write a temporary file inside the destination directory.
3. Flush, verify, and atomically rename it to its immutable path.
4. Append its artifact-index row idempotently.
5. Append the semantic ledger event idempotently.
6. Return the compact state update for checkpointing.

Crash rules:

- An artifact/index row without a semantic event is an orphan candidate, not a
  completed transition.
- A semantic event with a stale checkpoint is authoritative; resume
  reconciliation may rebuild the latest safe cursor from the event and refs.
- Repeating a completed publish with the same path and hash succeeds
  idempotently.
- Repeating it with different bytes fails closed.
- Checkpoint must never be the only owner of a completed fact.

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

Roots include:

- control manifests and configuration refs;
- ledger event refs;
- the latest nonterminal checkpoint refs;
- sealed recommendation manifests and any sealed attempt manifests that exist;
- review requests and decisions;
- retained externally delivered evidence exports.

Policies:

- `work/` is outside automatic cleanup and GC. An operator may remove it only
  by explicitly accepting that affected attempts can no longer resume.
- `reports/` may be removed and regenerated.
- intermediate checkpoints may be pruned according to section 14.
- immutable artifacts are deleted only when unreachable from every root and
  the session is terminal.
- GC first produces a dry-run manifest with paths, hashes, sizes, and reasons.
- Artifact-index compaction occurs only after artifact GC succeeds.

Because runtime refs are session-local, deleting another run or a global cache
cannot break the retained run.

## 20. Cutover Policy

Data v2 uses a clean cutover:

- no dual write;
- no fallback lookup of legacy flat paths;
- no hash-only payload/profile runtime branch;
- no `locate_session_data_dir` compatibility behavior for v2 commands;
- no automatic migration of old checkpoints, ledgers, indexes, or reports;
- no compatibility aliases for old report filenames or artifact types.

For this repository, the old local `data/` sessions and samples were determined
to have no remaining development value and were deleted with explicit user
authorization on 2026-07-15. The committed `data/` tree retains only the v2
`layout.json` marker and reference data still used by the application. Runtime
code must not recreate or silently mix legacy data into the v2 root.

## 21. Implementation Sequence

| Step | Status | Implementation state |
|---|---|---|
| 1. Add `layout.json`, run-root paths, ArtifactRef v2, and immutable writers | Complete | The repository and runtime use the strict v2 layout and run-relative, hash-verifiable immutable artifact references. |
| 2. Add ledger v2 envelope, event IDs, sequences, idempotency, and route events | Complete | Ledger v2 owns bounded lifecycle events and request-ID-bound review decisions. |
| 3. Introduce typed compact stage cursors and remove embedded attempt bodies from checkpoints | Complete | `stage_outputs` accepts only known stage keys and bounded recursive JSON cursors; the LangGraph channel rejects non-JSON values, oversized cursors, and embedded `FrozenAttemptSpec` bodies. Every resumable freeze/run lifecycle checkpoints the shared immutable attempt ref. Model-recommended Assembly/AutoHiC executions own it through `ParameterAttempt`; operator-directed Assembly review reruns bind it through `ReviewDecision` and `CandidateBundle`; non-parameterized lifecycles may own it directly. Same-node helper commands persist the same record before execution. |
| 4. Extract the shared parameter-recommendation persistence contract | Complete for shared persistence, Assembly, and AutoHiC adoption | Future stage recommenders, including T2T, must reuse the same storage helper; this step does not authorize new recommendation behavior. |
| 5. Persist exact model requests/responses and policy/knowledge references | Complete for the implemented parameter-recommendation path | The exact request sent to the model is persisted before invocation; durable records use refs instead of embedding repeated audit envelopes. |
| 6. Move reports out of the artifact index and generate shared report views | Complete | `current_status.json`, `assembly_review.md`, and `session_summary.{json,md}` are derived projections rather than workflow truth. |
| 7. Add storage audit, checkpoint compaction, and mark-and-sweep artifact GC | Partial | Read-only storage audit and a pure dry-run cleanup manifest are implemented. Terminal checkpoint compaction, deletion-performing artifact GC, artifact-index compaction after GC, and a unified `storage-audit` CLI are not implemented. Work cleanup is intentionally outside the design. |
| 8. Remove legacy layout data/code and validate the clean cutover | Partial | Legacy fallback behavior and the old local sessions/samples are removed. Real SQLite resume and existing-assembly-to-annotation entrypoint E2E tests exist; the outstanding clean-v2 acceptance path starts from raw reads and crosses read_qc/genome_survey/assembly before review. |

## 22. Confirmed Conformance Debt

The following gaps were confirmed against the current implementation on
2026-07-20. They are bounded follow-up work, not precedents for new stages.

| Priority | Area | Confirmed gap | Required ownership boundary |
|---|---|---|---|
| P0 | Artifact publication bypass | Shared atomic publishers and replay recovery exist, but several stage producers write a fixed path below `artifacts/` before calling `publish_artifact_records`. A conflicting replay mutates the published file before registration fails, leaving index/file hashes inconsistent. | Every durable producer writes under `work/` and crosses one atomic publish-and-register boundary; conflict tests must prove both the old file and index remain unchanged. |
| P1 | Resume transaction | CLI resume, the in-process interactive gate loop, and interactive review separately implement validation, annotation intake, checkpoint update, graph invocation, and lifecycle events. Their `update_state` failure handling and event envelopes differ. | One control-layer transaction owns state mutation and lifecycle event ordering; entrypoints own only input/output. |
| P1 | Whole-checkpoint budget | Stage cursor and resource-table budgets are enforced, but the 64 KiB target for the complete serialized `TopLevelState` and the 4 KiB active-interrupt budget are not yet enforced at one boundary. A 2 MB `review_notes` value is currently accepted by a memory checkpoint. | Add one whole-state checkpoint validator without reintroducing stage payload schemas or artifact bodies. |
| P1 | GC retention roots | Read-only reachability analysis exists, but checkpoint removal changes the root set and not every terminal/stage event carries the refs needed for audit replay. | Prove terminal audit reconstruction and artifact reachability after compaction before enabling deletion; keep work cleanup outside automatic GC. |
| P2 | Run identity | Several internal APIs accept a run-root path and a second session ID even though the run root already owns identity. | Pass a typed run context/`SessionPaths` internally; keep one session identity owner. |

The former Gene collection and checkpoint gaps were resolved on 2026-07-16:
collection now returns and consumes the durable published path, and Gene
checkpoints retain only immutable frozen-attempt and execution refs.

The former Assembly/AutoHiC attempt-ownership gap was resolved on 2026-07-17:
each `ParameterAttempt` now owns its exact frozen runner-input ref and execution
ref; method-specific current-stage duplicates were removed, and sealed manifests
retain those relations across retries.

The former review-recommendation, review-request-identity, and session-inspection
gaps were verified resolved on 2026-07-20. Recommendation files are scoped by
`review_request_id` and reference the canonical request without embedding it;
request bodies reject duplicate envelope fields; lifecycle classification reads
ledger/artifact facts and treats report files only as optional paths.

Reasonable dual representations remain allowed: raw output plus normalized
summary, immutable artifact plus metadata-only index, compact ledger event plus
artifact ref, and JSON report plus regenerable Markdown.

## 23. Acceptance Criteria

Data v2 is complete only when all of the following hold:

- A new run contains no repeated session ID below its run root.
- Every durable internal ref is session-relative and hash-verifiable.
- Copying a run to a different absolute path preserves audit replay.
- A blocked review resumes from the latest frontier using only checkpoint plus
  verified run-local refs.
- Removing checkpoint after terminal compaction does not prevent reconstruction
  of stage order, skip reasons, decisions, failures, and terminal outcome.
- Corrupting a referenced artifact causes replay/resume to fail closed.
- Replaying a node does not duplicate ledger effects or artifact-index rows.
- Recommendation rejection creates no execution attempt and consumes no retry
  budget.
- Assembly, Hi-C, and T2T parameter recommenders can share artifact cursors and
  storage helpers without sharing one biological/model DTO or stage-specific path rules.
- Checkpoint size budgets are enforced in tests.
- Reports can be deleted and regenerated without changing audit facts.
- A storage-audit command reports bytes by layer, largest checkpoint fields,
  duplicate content, unreachable artifacts, retained work bytes, and
  reclaimable report data.

## 24. Non-Goals

Data v2 does not provide:

- compatibility with old sessions;
- arbitrary historical checkpoint time travel;
- global hash-only runtime references;
- a parameter recommender that chooses methods or workflow stages;
- mutable evidence artifacts;
- report-driven workflow control;
- automatic reproduction of an external LLM response by calling the model
  again.
