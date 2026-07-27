# TUI Intake, Bootstrap, and Frozen Run Design

## Status and Scope

This design replaces the user-facing `ask` and `run` workflow with one
conversational `gene-agent tui` workflow. `gene-agent bootstrap` remains an
auxiliary machine-setup tool, not a normal workflow entry point.

The implementation covers assembly-oriented ONT and Hi-C sample intake,
bootstrap configuration already consumed by the repository, immutable run
input freezing, and durable TUI recovery. It deliberately excludes annotation
reference resources, download/install automation, directory scanning, multiple
environment profiles, remote scheduling, and in-TUI machine-configuration
commands. The only TUI command is `/resume`, which opens a session picker.

There is no AskPlan v1 migration, dual reader, or compatibility writer. A
session without a TUI draft is never adopted as an editable intake draft; an
existing durable review gate remains available through its existing review
path.

## Product Boundary

There are two operator-facing commands:

```text
gene-agent bootstrap [--update] [--apply]
gene-agent tui
```

`bootstrap` is a helper used before a workflow when the machine needs setup or
repair. `tui` is the application workflow: it collects a goal and the current
sample facts, lets the operator confirm the derived run inputs, freezes them,
and starts the internal executor. The executor is not a user-facing CLI
surface.

The ownership boundary is strict:

| Owner | Owns | Must not own |
|---|---|---|
| Bootstrap | Active `config/envs.yaml`, `.env` secrets, package conformance, machine validation | Sample paths, session manifest, run decisions |
| TUI | Goal, sample facts, draft manifest, planning choices, user confirmation, recovery routing | Machine readiness checks, machine configuration writes |
| Frozen executor | Immutable input manifest, run config, dependency snapshot | Live config lookup, draft mutation, input discovery |

TUI does not import `EnvResolver`, run package preflight, ask for package or
database paths, or decide whether the machine is ready. The freeze operation,
which happens only after the user confirms execution, creates a dependency
snapshot through a bootstrap-owned read-only service. If it cannot be created,
TUI reports the failure and directs the user to `gene-agent bootstrap`; it does
not turn into a configuration wizard.

## Bootstrap Tool

`config/envs.yaml` remains the one active machine-configuration source of
truth. Users do not hand-edit it as part of the normal workflow. `.env` remains
restricted to secrets and `GENE_AGENT_*` runtime overrides; it never stores
sample inputs or package/resource paths.

The existing `ProjectBootstrapPlan`, `EnvResolver`, package preflight, and
Juicer conformance machinery remain the implementation basis. Bootstrap keeps
its plan/apply split:

1. Inspect the active config and declare missing or invalid fields.
2. Ask only for concrete values needed to repair or initialize those fields.
3. Validate entered paths with the same runtime loaders and package canaries.
4. Show every proposed field change, redacting secrets.
5. Write and perform declared local conformance only with `--apply`.

Default `bootstrap` initializes or repairs missing/invalid fields and preserves
valid existing values. `bootstrap --update` presents the managed fields, lets
the operator explicitly choose a field to replace, shows `old -> new`, and
requires the same plan/apply confirmation. This exposes the existing
field-level overwrite approval mechanism rather than creating a second config
system.

Bootstrap may write the active config and `.env`, and may perform explicitly
declared local conformance such as the existing Juicer symlinks. It does not
download software, infer a missing install root, scan a directory for a
candidate, create annotation-resource configuration in this scope, or claim a
missing path is usable.

## TUI State Model

The current `AskPlanState` is not repurposed: annotation review still uses it
and annotation is outside this change. TUI receives an independent version-2
durable draft state named `TuiSessionDraft` (also exposed as
`TuiSessionState` for callers that describe the broader lifecycle). It is
persisted at:

```text
runs/<session-id>/control/tui_state.json
runs/<session-id>/control/manifest_draft.yaml
```

`tui_state.json` is the source of truth; `manifest_draft.yaml` is rebuilt from
confirmed facts after every accepted change and is therefore an inspectable,
mutable projection rather than an independent authority.

The state contains:

```text
version: 2
session_id
data_dir
parent_session_id?                 # only for a child rerun draft
request_text
intent: WorkflowIntent
intake: AssetCollectionState
selected_stages / methods / parameter fields
degradation_decision?
lifecycle: intake | planning | ready_to_confirm | frozen | running |
           blocked_review | terminal
```

`WorkflowIntent` is deliberately smaller than `EntryRequest`: it contains the
user's goal text, optional sample ID and target species, requested assembly
level, and requested stages. It does not require a manifest or source paths.

`AssetCollectionState` contains typed requirements, confirmed facts,
unresolved candidates, and an append-only answer history. Pending questions
are derived deterministically from requirements and confirmed facts, not saved
as a second mutable source of truth.

For this scope the confirmed fact types are:

```text
OntReadsFact(paths=[...])
HiCPairedReadsFact(pairs=[{r1, r2}, ...])
NoHiCFact()
```

Hi-C pairs remain explicit through the entire draft. A manifest schema update
adds a typed paired-read representation rather than relying on path-list
position or file-name inference. Runtime adapters can flatten a verified pair
into their legacy `[r1, r2, ...]` argument order only at the adapter boundary.

## Intake Conversation

TUI begins with a high-level goal and creates a draft session before any
manifest exists. It computes required sample facts from the parsed goal and
asks one missing role at a time. A focused question controls the default
meaning of an otherwise unlabelled answer; it does not restrict the answer to
one role.

For example, while asking for ONT reads, this answer may complete several
requirements at once:

```text
ONT: /data/chm13/ont/a.fastq.gz, /data/chm13/ont/b.fastq.gz
Hi-C R1: /data/chm13/hic/r1.fastq.gz; R2: /data/chm13/hic/r2.fastq.gz
```

The response pipeline is:

```text
raw answer
  -> deterministic extraction of exact path spans
  -> optional LLM fact candidates with role and source evidence
  -> reject paths absent from the raw answer
  -> local validation per candidate
  -> accept, reject, or retain unresolved candidates
  -> atomically persist state and rebuild manifest_draft.yaml
```

The LLM may identify several facts in one answer, but it cannot invent a path,
mark a path ready, infer a role from a filename, or replace a confirmed fact.
Only an explicit role label in the answer can confirm a non-focused role. A
proposed replacement becomes an explicit add/replace confirmation rather than
a silent overwrite.

Local validation is deterministic and bounded: each entered path must resolve
to an existing, readable, non-empty regular file and have a supported format
suffix for the requested role. TUI never expands a directory, scans for files,
or pairs reads based on names. A Hi-C pair requires explicit R1/R2 attribution
from the user response or a structured confirmation.

If the user says there is no Hi-C, TUI stores `NoHiCFact`, explains that the
plan must use the non-Hi-C/contig route, and requires an explicit degradation
confirmation before continuing. It does not represent the plan as complete
T2T scaffolding.

If LLM intent or answer parsing is unavailable, TUI remains usable. A small
deterministic intent recognizer and fixed question queue collect the goal and
required roles. Exact path extraction assigns unlabelled paths only to the
current focused role; explicit `ONT`, `Hi-C`, `R1`, and `R2` labels may
still complete other roles. Everything else remains unresolved rather than
guessed.

After intake completes, the existing stage, method, and parameter planner is
reused behind the TUI. Its inputs are the typed intent and the draft manifest,
not an early `EntryRequest`.

## Confirmation and Freeze

Before any execution, TUI presents the derived draft manifest and run config
for confirmation. Confirmation invokes one create-only freeze transaction that
writes these final control files:

```text
control/input_manifest.yaml
control/run_config.json
control/dependency_manifest.json
control/frozen_inputs.json
```

`input_manifest.yaml` replaces the current ambiguous session `manifest.yaml`
as the canonical execution input. The dependency manifest is a bootstrap-owned
snapshot containing the active config path and SHA-256, resolved environment
and package values needed by the selected stages, validation results, and a
capture time. It never includes `.env` secrets.

`frozen_inputs.json` is the commit marker. It records the SHA-256 for all three
input files, their schema versions, session identity, and optional parent
session identity. An executor accepts a session only when the marker and every
referenced file exist and all hashes match.

The transaction validates all draft inputs and stages temporary files in the
same filesystem. It publishes the final files followed by the marker. A
partially published set without a valid marker is not executable and is not
silently overwritten; recovery fails closed and reports the damaged state.
Repeating the identical freeze is idempotent. A different freeze attempt for an
already frozen session fails rather than modifying historical input.

The current overwrite-prone `materialize_run_layout` and the separate manifest
and run-config writes in `execute_confirmed_plan` are replaced by this single
transaction. The executor loads only the frozen files and dependency snapshot;
it must not reload live `config/envs.yaml` while running.

## Execution and Recovery

TUI invokes an internal `execute_frozen_session(session_id)` after a successful
freeze. This function verifies frozen hashes, constructs execution state from
the immutable inputs, and starts the existing graph/execution kernel. It is
not exposed as `gene-agent run` to normal users.

TUI itself can repeatedly create and execute sessions. Completing a run returns
to the TUI's top-level prompt; it never edits the completed session.

`/resume` presents a session picker and has three separate routes:

| Located session | TUI action |
|---|---|
| Draft, no valid frozen marker | Resume the same editable draft at the next missing requirement or planning step |
| Frozen and blocked at a human review gate | Enter the existing review/resume path with inputs read-only |
| Frozen terminal result | Create a new child draft with `parent_session_id`; copy confirmed assets as candidates and revalidate/reconfirm them |
| Frozen and actively executing | Refuse input edits and child creation |

The child rerun never changes the parent manifest, run config, hashes, review
decisions, or results.

## Repository Changes and Reuse

Reuse:

- `ProjectBootstrapPlan`, `apply_project_bootstrap_plan`, `EnvResolver`,
  package preflight, and Juicer conformance;
- structured LLM binding and exact path lexer from `intent.py`, refactored to
  produce `WorkflowIntent` and asset candidates instead of `EntryRequest`;
- existing stage/method/parameter planner after draft intake is complete,
  through a short-lived adapter rather than a persisted `AskPlanState`;
- session paths, durable review checkpoints, and execution graph.

Replace or remove from the user path:

- thin TUI delegation to `_create_ask_plan`;
- `EntryRequest` as the natural-language/intake contract;
- early manifest materialization and directory/file-name classification in
  `manifest_materializer.py`;
- direct user requests to edit a manifest or "proceed anyway" after failed
  input validation;
- user-facing `ask` and `run` commands;
- execution-time live `EnvResolver` lookup for frozen runs.

The `--config` override may remain as an internal/test injection mechanism, but
the product has no profile selection or `envs*.yaml` workflow.

## Acceptance Tests

The implementation must add focused tests for:

1. bootstrap initialization, repair, explicit update, no-change rerun, secret
   redaction, and no download/guessed-path behavior;
2. TUI initial goal with no paths, durable draft persistence, and restoration;
3. one response that supplies ONT and explicit Hi-C R1/R2 facts together;
4. rejection of a fabricated LLM path, directory path, nonexistent path,
   unsupported suffix, and filename-derived Hi-C pairing;
5. fixed-question operation when the LLM service fails;
6. `no Hi-C` requiring a confirmed degraded route;
7. a final manifest/run-config review followed by exactly one immutable freeze;
8. execution using the dependency snapshot after the live config changes;
9. all recovery routes, especially child rerun creation and immutable parent
   inputs;
10. rejection of old version-1 ask state rather than migration;
11. no public TUI path that asks for or writes machine configuration.

## Non-Goals

This change does not add annotation databases or models, installers or
downloaders, recursive path discovery, automatic biological classification,
remote execution, multi-profile configuration, or arbitrary in-TUI command
execution.
