# Phase 11 TUI And HPC Runtime Contract

**Status**: Current operator contract
**Updated**: 2026-07-13

## Purpose

The Phase 7 plan draft remains the planning UI, but it is no longer the final
parameter decision. Hifiasm parameters are produced at runtime after read QC,
genome survey, production EvidenceCase retrieval, and deterministic validation.
Every actionable initial or retry recommendation therefore enters a separate
hash-bound `parameter_approval` gate.

The conversational TUI has three progressive surfaces in one session:

1. **Plan Draft** — inputs, stages, methods, execution order, and known static
   parameters before the graph starts.
2. **Runtime Attempt Review** — the exact attempt, task, previous attempt,
   recommendation hash, parameter diff, rationale/failure facts, and selected
   production paper evidence.
3. **Execution And Terminal Status** — command/artifact/log references, attempt
   result, independent execution/review retry counts, and the next human gate or
   terminal outcome.

The Plan Draft must not claim to be the last parameter confirmation. Catalog
L2 fields such as hifiasm `kmer_length` and `purge_level` are shown as
`deferred`; only static execution settings (for example threads/read type) and
explicit user constraints belong in the initial plan. A user constraint on an
L2 field is labelled as a constraint, not a frozen command value.

This is a registry-driven contract rather than an assembly-only screen. The
initial plan lists every registered runtime human gate for the selected stages.
When a gate is actually reached, its artifact-backed proposal/evidence becomes
the next interaction in the same `gene-agent tui` conversation. Assembly
parameter recommendation is implemented today; assembly review can route a
quality failure back to that approval gate. Hi-C and T2T already have human
method/candidate review gates, while automatic parameter recommendation for
those stages remains explicitly `not_implemented` rather than being implied by
the draft.

## Runtime Flow

```text
gene-agent tui
  -> ask plan + manifest + run config
  -> read_qc / genome_survey
  -> production hifiasm paper cases ranked and truncated to top five
  -> LLM recommendation + deterministic validation
  -> blocked_human / assembly / parameter_approval
  -> TUI runtime attempt review
       approve_parameters -> freeze command -> execute
       reject_parameters  -> terminate without execution
  -> execution failure -> execution retry recommendation -> new approval
  -> review failure    -> review retry recommendation    -> new approval
  -> assembly review / downstream gates / terminal result
```

Retry reuses attempt 0's immutable profile, catalog, policy, and evidence. It
does not retrieve a second paper corpus. Execution and review retry budgets are
independent.

## Durable Remote/Cloud Use

The repository executes tools synchronously on the machine where the TUI is
started through the configured conda environment. This works when the operator
logs directly into a cloud/HPC test host; no Slurm/PBS submission model is
assumed by the conversational workflow.

When a batch run stops at a human gate, attach the TUI to the durable SQLite
session:

```bash
python3 -m app.entrypoints.run tui \
  --resume-session <session-id> \
  --data-dir <data-root>
```

The equivalent dedicated command is:

```bash
python3 -m app.entrypoints.run review \
  --session-id <session-id> \
  --data-dir <data-root>
```

For non-interactive operation, generate a decision template with `inspect
--write-decision-template`, review it, and pass it to `resume`. Parameter
approval templates bind `attempt_id`, `task`, `previous_attempt_id`, and
`recommendation_hash`; stale or edited bindings fail closed.

## Runtime Attempt Review Requirements

Before offering approve/reject, the operator view exposes:

- attempt ID, task, previous attempt ID, and recommendation hash;
- parameter changes and recommendation rationale;
- execution or review failure facts for retry attempts;
- evidence and recommendation artifact references;
- up to five selected production paper cases with DOI and exact Methods spans.

`stop` and `expert_review` dispositions do not enter `parameter_approval`.

## Remaining UI Work

The durable approval connection is implemented. A later presentation task may
add a persistent attempt timeline showing both retry budgets, command/log refs,
and execution status in one screen. Scheduler job IDs, queue state, preemption,
and cancellation require a separate scheduler adapter; they must not be
fabricated by the current synchronous runner.

## HPC Acceptance

Before treating an HPC smoke as complete, verify:

- the formal 10-paper production corpus loads and the selected top five reach
  the prompt with DOI/span/quote provenance;
- the first non-interactive run blocks before command freeze;
- a hash-bound approval resumes from SQLite and only then writes/executes the
  command;
- stale approval metadata is rejected;
- attempt artifacts, command/logs, checkpoint state, and ledger agree;
- at least one manual TUI pass covers parameter approval and a later human gate.
