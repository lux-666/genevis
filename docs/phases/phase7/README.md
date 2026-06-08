# Phase 7: Annotation Execution And The Review-Driven Annotation Agent

Status: planning. Subjects captured; specifics intentionally loose. These tasks
are the main objective for a sustained period, not a sprint.

## Why this phase

The assembly half of the workflow executes for real (Flye + QUAST mainline,
durable resume, assembly_review gate). The annotation half does not:

- `repeat_annotation` executes only RepeatMasker, and only against a
  **provided** library (`strategy == "provided"`); de novo library build
  (EDTA / RepeatModeler) and TRF are planning-only.
- `gene_annotation` has executable runners for Miniprot and AUGUSTUS (task 04
  Slice 1 + task 09 boilerplate consolidation), plus EVM merge and BUSCO
  (task 05). BRAKER3, MAKER, GeMoMa, and RNA-seq helpers remain planning-only.
  Real-data smoke validation is pending (task 08).
- `function_annotation` has an executable DIAMOND runner (task 06 Slice 1).
  InterProScan, eggNOG-mapper, and KofamScan remain planning-only. Real-data
  smoke validation and remaining method slices are pending (task 08).
- `assembly_review` has the full review-policy path, and `hic_scaffolding`
  already has a partial gate/resume path. The remaining downstream review points
  the workflow conceptually needs do not exist yet, and the existing gates still
  need to be pulled into one reusable registry.

Phase 7 makes the annotation half real **and** review-driven.

## Two threads, deliberately decoupled

1. **Execution landing** — give annotation methods real runners
   (runner + freeze + collect, mirroring `run_flye_attempt` / `run_yahs_attempt`
   + `LocalRunner`), flipping each method's registry `execution_support` from
   `planned_only` to `executable`.
2. **Review-chain landing** — stand up the chain of human/auto review gates the
   annotation agent needs, reusing the existing policy/timeout/recommender
   mechanism.

These are decoupled because a review gate can be exercised over a planning-only
method (a candidate can be reviewed before it can actually run). The review
chain can therefore go first, with execution slices filling in behind it.

## The unifying "wait time" gate model

There is one gate mechanism, not separate "auto" and "plan" modes. A gate's
behavior is a wait time plus a trigger:

- **wait 0** → auto-advance on the recommendation (`AUTO_DECIDE_NOW`).
- **wait ∞** → plan mode: block until a human decides (`BLOCK_INDEFINITE`).
- **wait N** → block with timeout, then auto-advance (`BLOCK_WITH_TIMEOUT`).
- **trigger** (`auto_decidable == False`) → force a human stop regardless of
  wait (`BLOCK_PENDING_CONFIRM`).

All four already exist in `app/review/policy.py` and are wired at least through
the current assembly-review path. Phase 7 extends one shared mechanism across
the annotation chain; it does not invent a new policy system.

## The three-agent framing (carried from Phase 6 task 06)

- **Assembly agent**: `inventory … assembly_review`. Owns plan 1 and the
  assembly→annotation handoff gate.
- **Annotation agent**: `hic_scaffolding` onward. It starts from the
  `annotation.intake_plan` produced at the Phase 6 task 06 handoff, then follows
  a sequence of plan→run→review steps, not one plan that runs to the end.
- **Interpretation agent** (agent 3): future, wholly new terminal capability.

Agent boundaries stay an implicit grouping over `STAGE_ORDER`; no agent field is
added to state or run-config in this phase.

## Tasks

See the numbered task docs in this folder:

- `01_review_gate_generalization.md` — make the gate/policy/recommender chain
  reusable beyond hard-coded `assembly_review` / `hic_scaffolding` branches.
- `02_repeat_mask_execution.md` — Dfam/provided-library RepeatMasker end-to-end
  + mask-completion review.
- `03_annotation_review_chain.md` — the full gate skeleton for the annotation
  path, consuming Phase 6 task 06's `annotation.intake_plan`.
- `04_gene_annotation_execution.md` — real gene-predictor runners, method by
  method.
- `05_gene_merge_final_set.md` — candidate merge (EVM/MAKER) + final gene set +
  BUSCO.
- `06_function_annotation_execution.md` — real function-annotation runners,
  method by method.
- `07_interpretation_agent.md` — direction note for agent 3 (out of scope until
  the annotation agent lands).
- `08_hpc_integration_smoke_tests.md` — HPC end-to-end smoke covering the full
  annotation chain (gene_annotation + function_annotation) with real Actinopterygii
  data, DIAMOND provenance, BUSCO lineage, and durable resume.
- `09_stage_method_boilerplate_consolidation.md` — behavior-preserving refactor:
  collapse per-method freeze/run/collect node + thin-router boilerplate into the
  shared `gene_annotation` helper + `{suffix: cfg}` table pattern.
- `10_natural_language_draft_plan_approval.md` — refactor ask/TUI entry into a
  real natural-language -> draft plan -> accept/reject -> manual-correction
  flow.

## Annotation review chain (target shape)

```
assembly_review            (can this assembly continue downstream?)
  ↓
annotation intake plan     (Phase 6 task 06 plan 2)
  ↓
hic_scaffolding            (optional annotation-agent local gate)
  ↓
repeat_annotation          (mask against existing/Dfam-derived library)
  ↓
mask review                (what ran/didn't; recommended gene input)
  ↓
gene-method planning review(which gene method(s) actually run)
  ↓
run gene method(s)         (produce candidate gene models)
  ↓
candidate-evidence review  (candidates done, not yet merged)
  ↓
merge (EVM/MAKER)          (EVM weights decided here, not earlier)
  ↓
final gene-set review + BUSCO
```
