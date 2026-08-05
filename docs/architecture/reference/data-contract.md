# Run Data Contract

> **Status:** implemented; see [Architecture conformance](conformance.md) for
> explicit gaps.  
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

**Compatibility:** No legacy-session read, resume, migration, or dual-write contract

## 1. Purpose

Gene Agent runs long-lived biological workflows with parameter recommendation,
human review, retries, large tool outputs, and durable resume. The data model
must make those workflows auditable without allowing the same mutable fact to
spread through artifacts, the artifact index, ledger events, checkpoints, and
reports.

Data v2 establishes one shared contract for all stages. The implemented
Assembly, AutoHiC, and T2T parameter-recommendation loops use the same
persistence contract rather than stage-specific audit envelopes.

This specification supersedes earlier data-layout and persistence assumptions.
Biological and stage contracts remain governed by their current specifications.

## 2. Product Decisions

1. The project is still in development. Data v2 does not preserve compatibility
   with old sessions.
2. Each `runs/<session_id>/` directory is the logical ownership boundary. Its
   run-owned audit and decision evidence must not depend on another session or
   a global content-addressed object store; external inputs remain explicit
   dependencies.
3. Durable resume targets the latest safe frontier, especially a blocked human
   gate. Arbitrary historical-node time travel is not a product requirement.
4. Existing report filenames and artifact types are not external compatibility
   contracts.
5. “Planner” in current parameter code means a stage parameter recommender. It
   does not mean method selection or parent-graph planning.
6. Public deterministic runs use `gene-agent run --manifest PATH --run-config
   PATH --data-dir PATH [--non-interactive]`. The manifest solely owns the
   session identity; `data-dir` owns layout placement at
   `runs/<session_id>/`. The run config must explicitly contain JSON arrays for
   `requested_stages` and `local_exec`. `requested_stages` must not be `null`;
   `[]` is the valid empty biological-stage plan. Internal/TUI compatibility
   state may retain `None`, but the public structured boundary rejects it before
   freeze or execution.

## 3. Replay Terminology

Data v2 distinguishes two forms of replay:

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
- Both public structured runs and confirmed TUI plans execute only from frozen
  `input_manifest.yaml` and `run_config.json`. TUI-only `manifest_draft.yaml`
  and `tui_state.json`, when present, are mutable intake records and never
  executor authority. There is no mutable `manifest.yaml` execution authority.
- `frozen_inputs.json` binds the hashes of `input_manifest.yaml`,
  `run_config.json`, and `dependency_manifest.json` and is published last.
- `control/run.json` is an accepted future metadata owner but is not currently
  created; see the replay-capability conformance entry.
- Stage directories never repeat `<session_id>`.
- Numeric recommendation and attempt directories use zero-padded, stage-local
  IDs such as `000`, `001`, and `002`.
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

An operator-directed Assembly rerun at the review gate has a different
parameter source and is outside that `ParameterAttempt` ownership rule. Its
review decision owns the operator-supplied parameters and schedules a compact
rerun request. The shared direct-attempt cursor then owns the exact frozen and
execution refs through freeze and run; collect publishes the resulting
candidate bundle. This path must not invoke the model again or synthesize
recommendation/approval evidence solely to imitate the recommendation
lifecycle.

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
- bounded stage-owned review projections needed for routing or display, such as
  Assembly Review candidate, signal, and metric summaries;
- a bounded diagnostic route tail.

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

## 16. Central Path Ownership

Only the storage layer constructs run paths. The current `SessionPaths` surface
used directly or through stage/storage helpers is:

```python
paths.stage_artifacts_dir("assembly")
paths.artifact_path("assembly", "parameter_recommendations", "000", "...")
paths.artifact_path("assembly", "attempts", "000", "...")
paths.artifact_path("reviews", review_request_id, "request.json")
paths.stage_work_dir("assembly")
paths.work_path("assembly", "attempts", "000", "...")
paths.report_path("session_summary.json")
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
- A semantic event with a stale checkpoint remains authoritative audit truth.
  Current durable resume still requires the retained checkpoint, except for
  narrowly implemented stage/evidence replay recovery; there is no generalized
  Ledger-to-checkpoint reconstruction path.
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

## 20. Compatibility Boundary

Data v2 has one current representation:

- no dual write;
- no fallback lookup of legacy flat paths;
- no hash-only payload/profile runtime branch;
- no `locate_session_data_dir` compatibility behavior for v2 commands;
- no automatic migration of old checkpoints, ledgers, indexes, or reports;
- no compatibility aliases for old report filenames or artifact types;
- runtime code must not recreate or mix legacy data into a v2 run root.

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
- Terminal compaction preserves reconstruction of stage order, skip reasons,
  decisions, failures, and terminal outcome while retaining one standalone
  checkpoint.
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
debt, not a reduction of the Data v2 contract.
Genome Survey k-mer handoffs are typed `ArtifactRef` values in the checkpoint
cursor and in frozen method dependencies; legacy path strings are rejected at
the stage boundary.
