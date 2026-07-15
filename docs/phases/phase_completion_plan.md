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
5. Operator usability pack: inspect JSON, next-action projection, decision
   templates, and blocked/failed operator report artifacts.
6. Provided gene annotation adoption.
7. Assembly review evidence expansion, BUSCO first.
8. Assembly parameter loop and candidate comparison.
9. Assembly review visual report and human selection.
10. Function annotation provenance completion.

Phase 5 is gated on the Phase 4 real-data acceptance pass (Flye + QUAST)
being recorded in `docs/handoff.md` before broader tool execution starts.
The remaining Phase 5 order intentionally prioritizes operator usability,
assembly-review evidence, controlled assembly rerun loops, visual human review,
and provided/adopted annotation completion before broader tool execution.
Deferred items live in `docs/phases/phase5/README.md`; do not duplicate that
catalog here.

## Phase 6: Observability and Durable Operation

Goal: make runs auditable and resumable for long projects.

Tasks in `docs/phases/phase6/`:

1. Read QC completion and shared assembly parameters (task 01, completed).
2. HPC end-to-end acceptance: single session from raw reads to `done` with
   durable resume at every human gate.
3. Interactive human gate and resume CLI: let operators inspect, choose, and
   resume supported human gates from an interactive CLI without hand-editing
   decision JSON or assembling the command sequence.
4. Session observability and run trace: structured stage-by-stage trace
   artifact from ledger data, exposed via `gene-agent inspect --trace`.
5. TUI initial ask plan preview: show a detailed early plan preview that
   matches the final `Run Plan` confirmation style before accepting an
   auto-resolved ask plan.
6. Replan after assembly review (assembly→annotation agent boundary): treat the
   `assembly_review` gate as the handoff from a virtual assembly agent to a
   virtual annotation agent, where the continue-downstream decision is the
   annotation agent making its own intake plan (stages, methods, parameters, or
   bounce back for a rerun). A future interpretation agent is noted but out of
   scope.
7. Hardening beyond the current sqlite resume minimum, including optional
   postgres/backend work if long-running deployments require it.
8. Lifecycle commands beyond the current structured review resume path, such as
   pause, retry, abort, or rebuild, once their semantics are explicitly scoped.

Exit criteria:

- A stopped session resumes from checkpoint and ledger state without
  duplicate side effects.
- User-facing reports explain what was run, what was skipped, and what
  requires human attention.

## Phase 7: Annotation Execution And The Review-Driven Annotation Agent

Goal: make the annotation half of the workflow real. Today `gene_annotation`
and `function_annotation` are candidate-only, `repeat_annotation` executes only
RepeatMasker against a provided library, `assembly_review` has the full review
path, and `hic_scaffolding` already has a partial gate/resume path. Phase 7
turns the downstream stages into a **review-driven annotation agent**: real tool
execution behind a chain of review gates, each gate using one shared
policy/recommender/resume mechanism.

This phase carries the three-agent direction recorded in Phase 6 task 06: the
`assembly_review` gate is the assembly→annotation handoff, it produces the
`annotation.intake_plan`, and the annotation agent owns everything from
`hic_scaffolding` onward as a sequence of plan→run→review steps rather than one
plan that runs to the end.

Unifying idea (the "wait time" model): there are not two separate "auto" and
"plan" modes. There is **one gate mechanism** with a configurable wait. Wait 0 =
auto-advance on the recommendation; wait ∞ = plan mode (block until a human
decides); wait N = block with timeout then auto-advance. A per-gate trigger
(`auto_decidable=False`) forces a human stop regardless of wait. All of this
already exists in `policy.py` (`AUTO_DECIDE_NOW` / `BLOCK_INDEFINITE` /
`BLOCK_WITH_TIMEOUT` / `BLOCK_PENDING_CONFIRM`) and is wired through the current
assembly-review path; Phase 7 extends it across the annotation chain and folds
the existing `hic_scaffolding` gate into the same registry.

Tasks in `docs/phases/phase7/` (subjects first; specifics deferred):

1. Generalize the review-gate mechanism beyond hard-coded `assembly_review` /
   `hic_scaffolding` branches: make the policy/timeout/recommender/resume chain
   reusable so any annotation gate can declare a recommender prompt bundle and an
   `auto_decidable` trigger.
2. Repeat/mask execution end-to-end: run RepeatMasker against an existing repeat
   library (e.g. Dfam-derived) to produce a masked genome for a real genome,
   plus a mask-completion review gate that reports what ran, what did not, and
   the recommended gene-annotation input.
3. The annotation review chain: consume Phase 6 task 06's
   `annotation.intake_plan`, then stand up the gate skeleton for the full path —
   mask review → gene-method planning review → candidate-evidence review →
   merge/EVM-weight review → final-gene-set + BUSCO review — initially over
   candidate-only methods so the chain is exercised before execution lands.
4. Gene annotation execution slices: real runners for the gene predictors one
   method at a time (e.g. an ab-initio path first), mirroring the frozen-attempt
   runner pattern, each flipping its registry `execution_support` to executable.
5. Candidate merge and final gene set: EVM/MAKER-style merge of candidate gene
   models, gated by the candidate-evidence and merge reviews, producing a final
   gene set validated by BUSCO.
6. Function annotation execution slices: real runners for InterProScan / DIAMOND
   / eggNOG-mapper / KofamScan one method at a time, with database provenance
   gating retained.
7. (Stretch / direction) Interpretation agent (agent 3): a new terminal
   capability that synthesizes assembly + annotation outputs into a biological
   interpretation. Out of scope until the annotation agent lands.

Exit criteria:

- A real genome can run assembly → masked genome → at least one executed gene
  prediction → at least one executed function annotation, through the review
  chain, with each gate honoring its wait/trigger policy.
- Every newly executed method runs through the same runner/freeze/collect pattern
  and durable resume as the assembly mainline.
- The annotation review gates reuse one shared mechanism, not per-stage forks.

## Phase 8: Inventory Alignment, Assembly Methods, And T2T Stage

Goal: correct the stage boundary around assembly-era work. `inventory` remains
the first stage and owns input profiling. `assembly` remains the long-read
candidate stage and should evolve from a Flye-first path into a method-dispatch
stage. `hic_scaffolding` remains the existing chromosome scaffolding stage.
Phase 8 adds one new top-level stage: `t2t`, placed after `hic_scaffolding` and
before repeat annotation.

Current status: an earlier Slice 1 implemented base assembly route-selection
schema and knowledge-pack loading, but the Phase 8 documents drifted into an
incorrect design that treated input profiling, Hi-C curation, and T2T finishing
as assembly-internal agents. The docs are being corrected first so
implementation follows the real stage model.

Compatibility check: commit `ca685a9` does not conflict with this corrected
model. Its route-selection slice currently lives inside `assembly` and should be
kept as a compatibility prelude until `inventory` exposes broader profile hints.
The next assembly refactor should replace the Flye-specific method path after
route selection, not revert the route-selection gate. Commit `eb1c838` is
limited to Phase 7 function-annotation metadata/docs cleanup and does not affect
Phase 8 stage boundaries.

Phase 8 MVP is intentionally small:

1. Strengthen `inventory` outputs with data availability, target level, and
   assembly hints.
2. Refactor `assembly` so method application is not hard-coded to Flye; Flye
   becomes one method branch, with hifiasm-ONT / hifiasm-HiFi next.
3. Keep `assembly_review` contracts stable.
4. Keep Hi-C work under existing `hic_scaffolding`, adding clearer final
   assembly refs for downstream T2T when needed.
5. Add the `t2t` stage, including telomere checks, ONT-backed extension
   candidates, conservative automatic patching, and patched-assembly handoff.

Out of scope for Phase 8 unless a later slice explicitly adds consuming nodes:
renaming `inventory`, creating a parallel Hi-C stage inside assembly, real Verkko
execution, HERRO correction, Dorado/Medaka polishing, Merqury QV, Teloclip, or
TeloComp consensus-based genome modification.

Remaining Phase 8 work lives in `docs/phases/phase8/01_*.md` through
`06_*.md`: schema/profile deltas, inventory enhancement, assembly method
dispatch, existing `hic_scaffolding` alignment, new `t2t` stage design, and final
stage wiring cleanup.
