# 06 Phase 4 Status Docs And Phase 5 Gate

Task status: planned.

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

- [ ] Update `README.md` so "Current Implementation" matches the current
  realized local execution slices.
- [ ] Update `docs/handoff.md` "Next Best Task" to point to task 04 until the
  real-biology mainline acceptance pass is complete.
- [ ] Remove or correct stale risk notes that contradict the Phase 4 env
  dispatcher implementation.
- [ ] Add a concise Phase 4 status table:
  - implemented in code
  - verified by committed fixture
  - requires human real-data smoke
  - deferred
- [ ] Add a Phase 5 entry gate that requires task 04 evidence in the artifact
  trail.
- [ ] Record durable resume as a Phase 6 / lifecycle prerequisite, not a Phase 4
  implementation task.
- [ ] Run documentation sanity checks.

## Acceptance

- A new agent can read the docs and correctly identify the next best task.
- README and handoff no longer disagree about the current implementation.
- Phase 4 docs clearly separate fixture smoke from real-biology acceptance.
- Phase 5 is blocked on real Phase 4 smoke evidence, not just unit tests.

## Verification

```bash
git diff --check
```
