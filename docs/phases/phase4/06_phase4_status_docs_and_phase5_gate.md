# 06 Phase 4 Status Docs And Phase 5 Gate

Task status: implemented.

## Goal

Make the repository's status documents agree with the real implementation state
and define an explicit gate before Phase 5 execution expands.

The project has moved faster than some docs. This task prevents future agents
from following stale README or handoff notes and accidentally broadening method
execution before the Phase 4 real-biology mainline is accepted.

## Scope

- Reconcile Phase 4 status across:
  - `README.md`
  - `docs/handoff.md`
  - `docs/phases/phase4/README.md`
  - task files under `docs/phases/phase4/`
  - `docs/phases/phase_completion_plan.md` if phase boundaries need wording
    updates
- Record which execution slices are real, opt-in, planning-only, or deferred.
- Add a clear Phase 5 entry gate.
- Keep durable resume, RemoteRunner, requested-stage routing, and annotation
  database downloads as explicit later decisions unless separately approved.

## Boundaries

- Do not change code in this task unless a doc claim exposes a small typo in a
  public command.
- Do not rewrite older phase history; add current status and correction notes.
- Do not mark Phase 5 as started until task 04 succeeds on real data.
- Do not claim durable cross-process resume exists while the graph uses the
  memory checkpointer.

## Tasks

- [x] Update `README.md` so "Current Implementation" matches the current
  realized local execution slices.
- [x] Update `docs/handoff.md` "Next Best Task" to point to task 04 until the
  real-biology mainline acceptance pass is complete.
- [x] Remove or correct stale risk notes that contradict the Phase 4 env
  dispatcher implementation.
- [x] Add a concise Phase 4 status table:
  - implemented in code
  - verified by committed fixture
  - requires human real-data smoke
  - deferred
- [x] Add a Phase 5 entry gate that requires task 04 evidence in the artifact
  trail.
- [x] Record durable resume as a Phase 6 / lifecycle prerequisite, not a Phase 4
  implementation task.
- [x] Run documentation sanity checks.

## Acceptance

- A new agent can read the docs and correctly identify the next best task.
- README and handoff no longer disagree about the current implementation.
- Phase 4 docs clearly separate fixture smoke from real-biology acceptance.
- Phase 5 is blocked on real Phase 4 smoke evidence, not just unit tests.

## Verification

```bash
git diff --check
```

## Implemented State

- `README.md` "Current Implementation" and "Not implemented" blocks now
  describe the realized Phase 3/4 slices: real `read_qc` / `genome_survey`
  (Jellyfish+GenomeScope2 opt-in) / `assembly` (Flye opt-in) /
  `assembly_review` (QUAST opt-in + action provenance) / `hic_scaffolding`
  (YaHS opt-in) / `repeat_annotation` (RepeatMasker opt-in); planning-only
  `gene_annotation` and `function_annotation`; shared `EnvResolver` +
  `env_dispatch` wired into five of six stages (`genome_survey` excepted);
  and CLI `--local-exec` + `--run-config` structured entry. The "Read First"
  list now includes Phase 3/4 README so a new agent lands on the current
  status doc.
- `docs/phases/phase4/README.md` gained a "Phase 4 Implementation Status"
  table and a "Phase 5 Entry Gate" section that makes Phase 5 startup
  contingent on the task 04 artifact trail being recorded in
  `docs/handoff.md`.
- `docs/phases/phase_completion_plan.md` Phase 5 now carries an explicit
  entry-gate note pointing back at `docs/phases/phase4/README.md`, and
  Phase 6 now restates that durable cross-process resume is a Phase 6
  deliverable (sqlite/postgres checkpointer + lifecycle commands), not a
  Phase 4 prerequisite.
- `docs/handoff.md` status entries now include a Phase 4 task 06 bullet;
  the stale Known Risk claiming that `assembly` / `assembly_review` /
  `hic_scaffolding` / `repeat_annotation` had not been rewired through
  `env_dispatch` was corrected (only `genome_survey` still needs that
  wiring); the "Files Most Likely To Change In Phase 2" section is
  replaced by "Files Most Likely To Change In Phase 5"; the Read Order
  now includes Phase 3 and Phase 4 READMEs.
- No code files were touched. Task 06 is doc-only by design and by
  scope.
