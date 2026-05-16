# Phase Completion Plan

Long-range implementation roadmap. `docs/handoff.md` records current
status and the next best task. `docs/stage_contracts.md` defines stage
behavior. Operational phase/task handoffs live under `docs/phases/phase*/`.

## Completed Phases

Each completed phase has its own retrospective and per-task records under
`docs/phases/`. Read the phase folder when you need history; do not
restate it here.

- **Phase 0 — Scaffold and Contracts.** Repository shape, `TopLevelState`
  contract, stage order, descriptor roots, structure tests.
- **Phase 1 — Parent Graph and Inventory MVP.**
  `docs/phases/phase1/phase1.md`. Real manifest → asset registry → ledger,
  inventory routing for success / review / existing-assembly / failure.
- **Phase 2 — CycloneSEQ Contracts and First Method Slice.**
  `docs/phases/phase2/phase2.md` plus `01_*.md` … `11_*.md`. Full
  CycloneSEQ contract through function annotation, with the SeqKit stats
  vertical slice as the first executable method.
- **Phase 3 — Natural Entry, Review Resume, and Real Execution.**
  `docs/phases/phase3/README.md`. `gene-agent ask` natural-language entry,
  in-process review resume helpers, and stage-by-stage realization.
- **Phase 4 — Real Local Assembly Mainline.**
  `docs/phases/phase4/README.md`. Per-stage conda env (`config/envs.yaml`
  + `EnvResolver` + `env_dispatch`), real Flye / QUAST mainline, structured
  `--run-config`, server `git pull` adaptation, real-data acceptance run
  recorded in `docs/handoff.md`.

## Phase 5: Workflow Hardening And Downstream Backbone

Tasks in `docs/phases/phase5/`:

1. Execution consistency and server gate (Phase 5 task 01 wired
   `genome_survey` through `env_dispatch`; server-side validation is
   recorded in `docs/handoff.md`).
2. Human gate and durable resume minimum.
3. Existing assembly to RepeatMasker backbone.
4. Review policy: strict / auto modes and LLM recommendation.

Phase 5 is gated on the Phase 4 real-data acceptance pass (Flye + QUAST)
being recorded in `docs/handoff.md` before broader tool execution starts.
Deferred items live in `docs/phases/phase5/README.md`; do not duplicate
that catalog here.

## Phase 6: Observability and Durable Operation

Goal: make runs auditable and resumable for long projects.

Tasks:

- User/developer notebook traces.
- sqlite/postgres checkpointer for cross-process resume.
- Lifecycle commands: pause, resume, retry, abort, review.
- Database version provenance for annotation resources.

Exit criteria:

- A stopped session resumes from checkpoint and ledger state without
  duplicate side effects.
- User-facing reports explain what was run, what was skipped, and what
  requires human attention.
