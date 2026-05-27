# 03 Interactive Human Gate And Resume CLI

Task status: not started.

## Goal

Make blocked human-review sessions operable from an interactive CLI flow instead
of requiring the operator to piece together `inspect`, decision-template editing,
and `resume --decision-file` commands by hand.

The target operator loop is:

```text
blocked_human session
-> interactive human-gate/resume CLI
-> show the pending review request and candidate choices
-> operator selects or enters the required decision fields
-> CLI writes the same structured decision artifact/request
-> CLI resumes through the existing durable sqlite path
-> workflow continues from the same checkpoint
```

This is a usability task over the existing review/resume contract. It should not
replace `ResumeReviewRequest`, bypass review validation, or create a second
resume implementation.

## Current Problem

Phase 5 made durable review resume work across processes. Phase 6 task 02 proved
that both `gene-agent run` and `gene-agent ask` can complete end-to-end on HPC
with sqlite resume. The operator flow is still too manual:

```bash
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --json
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --write-decision-template /absolute/path/to/decision.json
gene-agent resume --session-id <SESSION> --data-dir /absolute/path/to/data --decision-file /absolute/path/to/decision.json --checkpointer sqlite
```

That is acceptable for a smoke script, but not for routine operation. A user who
is already at a blocked human gate should be able to choose among the allowed
options, confirm required refs/notes, and continue without memorizing the command
sequence or hand-editing JSON.

## Relationship To Earlier Tasks

- Phase 3 task 02 defines structured inspect and resume semantics.
- Phase 5 task 02 implements durable sqlite `inspect` / `resume`.
- Phase 5 task 05 adds operator summaries, next-action projection, and decision
  template generation.
- Phase 6 task 02 validates the manual durable resume loop in a full HPC run.

This task builds the interactive operator surface on top of those pieces. It
should reuse the same inspection summary, decision requirements, decision-file
schema, ledger events, artifact writes, and durable checkpoint resume path.

## Scope

- Add an interactive CLI path for pending human gates. The final command name is
  an implementation detail, but the UX must support an obvious flow such as:
  - `gene-agent human-gate --session-id ... --data-dir ...`
  - or `gene-agent resume --interactive --session-id ... --data-dir ...`
- Support the existing durable human-gate stages:
  - `assembly_review`
  - `hic_scaffolding`
- Load the blocked session using the same durable inspect/checkpoint state used
  by current `inspect` and `resume`.
- Present allowed decisions, candidates, candidate refs, method choices, report
  refs, and notes needed for the active gate.
- Let the operator choose decisions through numbered options and enter required
  free-text fields such as notes, rationale refs, or action-output refs.
- Generate the same structured resume request accepted by
  `app.review.base.ResumeReviewRequest`.
- Validate the generated request before mutating checkpoint, artifacts, or
  ledger.
- Persist the generated decision payload or artifact path so the decision remains
  auditable after the interactive session exits.
- Resume by calling the existing durable resume path rather than duplicating
  state mutation logic.
- Provide a non-interactive or dry-run mode suitable for tests and wrappers,
  without making users assemble the full resume command manually.
- Keep the old `inspect --write-decision-template` and `resume --decision-file`
  path available for scripts.

## Non-Goals

- Do not change review decision semantics.
- Do not add automatic biological acceptance, purge, polish, rerun, or Hi-C
  contact-map thresholds.
- Do not use an LLM to choose review decisions.
- Do not implement retry, abort, rebuild, or broad lifecycle operations.
- Do not build a web UI.
- Do not introduce a second checkpoint backend or postgres work.
- Do not remove the explicit structured decision-file path used by automation.
- Do not store metric tables, command logs, FASTA/FASTQ contents, or large
  reports in graph state.

## Operator Experience Requirements

For a blocked `assembly_review` gate, the CLI should show:

- session id, current stage, and supervision state;
- review request/report refs;
- allowed decisions;
- candidate ids and final assembly refs;
- candidate status and evidence refs when available;
- notes explaining which actions require explicit output refs or provenance;
- prompts for required fields based on the chosen decision.

For a blocked `hic_scaffolding` gate, the CLI should show:

- planned scaffold method choices, if any;
- the pre-Hi-C assembly fallback used when skipping;
- contact-map/manual-review policy requirements;
- allowed skip/select decisions and required notes.

The CLI should fail clearly when:

- no durable checkpoint exists for the session;
- the session is not currently blocked on a supported human gate;
- the checkpoint session id does not match the CLI session id;
- the generated request fails existing resume validation;
- required candidate refs or action-output refs are missing.

## Data And Audit Requirements

- Interactive choices must produce a structured decision payload that can be
  inspected later.
- The final decision must pass the same validation as a hand-written
  `--decision-file`.
- Ledger events should identify that the source was an interactive CLI flow.
- A failed interactive validation must not update checkpoint state.
- A successful interactive resume must not duplicate prior method attempts.
- The generated decision payload should include enough operator notes or metadata
  to explain why the decision was made.

## Tasks

- [ ] Decide the final CLI spelling for the interactive flow.
- [ ] Add parser support for the interactive human-gate/resume command or flag.
- [ ] Reuse durable inspect output to load active interrupt, decision
  requirements, candidate refs, report refs, and next-action context.
- [ ] Add prompt rendering for `assembly_review` decisions.
- [ ] Add prompt rendering for `hic_scaffolding` decisions.
- [ ] Build a `ResumeReviewRequest` from operator selections.
- [ ] Validate the request through the existing resume validation path before
  mutation.
- [ ] Persist the generated decision payload or decision-file artifact.
- [ ] Resume through the existing durable sqlite resume implementation.
- [ ] Preserve the current explicit decision-file workflow for scripts.
- [ ] Add tests for successful assembly-review selection, invalid input,
  unsupported stage, missing checkpoint, declined/cancelled interaction, and no
  duplicate side effects after resume.
- [ ] Add tests for Hi-C skip/select prompts and required contact-map policy
  notes.
- [ ] Update `README.md` and `docs/handoff.md` with the new operator workflow.
- [ ] Validate the interactive path against an existing Phase 6 blocked-session
  smoke or a fresh HPC smoke.

## Acceptance

This task is complete only when:

- an operator can inspect, decide, and resume a blocked `assembly_review` session
  from one interactive CLI flow;
- an operator can handle a blocked `hic_scaffolding` session from the same
  interactive surface;
- invalid or incomplete interactive choices fail before checkpoint mutation;
- successful interactive resume records auditable decision artifacts and ledger
  events;
- the old `inspect --write-decision-template` plus `resume --decision-file`
  workflow still works for automation;
- tests cover the prompt/request construction path without depending on a live
  terminal;
- an HPC or durable sqlite smoke shows the interactive flow does not duplicate
  prior attempts after resume.

## Handoff Record Requirements

After validation, update `docs/handoff.md` with:

- command used for the interactive flow;
- session id and data dir used for validation;
- human gate stage exercised;
- decision selected and generated decision artifact/ref;
- resume result and terminal/current state;
- duplicate-attempt check result;
- any UX gaps or follow-up tasks.
