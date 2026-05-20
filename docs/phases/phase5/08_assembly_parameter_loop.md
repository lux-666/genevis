# 08 Assembly Parameter Loop And Candidate Comparison

Task status: implemented.

## Goal

Make the first half of the workflow iterative: users should be able to adjust
assembly parameters, rerun controlled candidates, compare evidence, and choose
the best assembly through a human gate.

The target loop is:

```text
read_qc / genome_survey evidence
-> assembly candidate run with explicit parameters
-> assembly_review evidence bundle
-> human chooses accept / adjust parameters / stop
-> adjusted candidate run
-> candidate comparison report
-> human selects final assembly
```

## Scope

- Add a structured parameter-adjustment request for assembly reruns.
- Start with Flye only; do not add NextDenovo, Canu, or hifiasm execution.
- Treat every rerun as a new candidate with explicit parameter provenance.
- Preserve all previous candidates and review evidence.
- Compare candidates from artifact refs and compact metrics; do not store large
  outputs in graph state.
- Support durable resume from the review gate into a controlled rerun.
- Keep rerun count and resource use explicitly bounded by the user's decision or
  run-config.

## Non-Goals

- Do not implement broad retry/rebuild lifecycle commands.
- Do not automatically search a parameter grid.
- Do not automatically pick the best assembly from metrics.
- Do not delete old candidates or overwrite prior artifacts.
- Do not add new assemblers in this task.

## Expected Outputs

- A decision-file/action contract for `adjust_assembly_parameters`.
- Candidate IDs that include run attempt identity and parameter provenance.
- Ledger events for rerun requested, rerun started, rerun completed/failed, and
  candidate comparison generated.
- Candidate comparison artifact that lists assembly parameters and available
  QUAST/BUSCO signals side by side.
- Tests proving reruns do not restart from inventory, do not overwrite prior
  candidates, and re-enter assembly review with all candidate refs available.

## Tasks

- [x] Define the review decision schema extension for assembly parameter
  adjustment.
- [x] Add safe validation for Flye rerun parameters.
- [x] Route accepted adjustment decisions back into an assembly rerun without
  rebuilding inventory/read QC artifacts.
- [x] Store each rerun as a distinct candidate with parameter provenance.
- [x] Re-run assembly review evidence for the new candidate.
- [x] Generate a candidate comparison artifact.
- [x] Add tests for valid rerun, invalid parameters, duplicate side-effect
  prevention, candidate preservation, and comparison generation.

## Acceptance

- A human can request one or more explicit Flye parameter adjustments from the
  assembly review gate.
- The workflow preserves all candidates and evidence.
- The user can compare candidates before choosing a final assembly.
- The loop remains auditable and bounded; there is no hidden auto-optimization.

## Implementation Key Points

- Rerun parameters are carried in `ResumeReviewRequest.metadata["rerun_parameters"]`
  (no schema break to `ResumeReviewRequest`).
- `_apply_rerun_decision` in `assembly_review/nodes.py` validates Flye params,
  executes inline, writes a new candidate bundle, and returns state that
  re-enters the assembly_review subgraph from scratch.
- Rerun replay is idempotent across the resume/checkpoint crash window: an
  existing completed `flye_rerun_<N>_candidate.json` is treated as the rerun
  commit marker and reused when parameters/source candidate match. Failed or
  partial rerun directories occupy their attempt index, so the next adjustment
  advances to a fresh `rerun_<N+1>` path instead of overwriting evidence or
  requiring manual cleanup.
- Rerun resume decisions require `candidate_id` to match a known review
  candidate, preserving source-candidate provenance.
- Re-entry works via the parent graph loop: the handler sets
  `current_stage = "assembly_review"` + `supervision_state = "running_stage"`,
  so `route_controller` → `route_from_current_stage` re-invokes the subgraph.
- `resolve_candidates` picks up the new candidate from
  `stage_outputs["assembly"]["selected_candidate_refs"]` naturally.
- `generate_candidate_comparison` node runs after `evaluate_rules` when 2+
  candidates exist; writes a comparison JSON artifact with per-candidate
  parameters and available QUAST/BUSCO metrics. If the existing comparison ref
  already matches the current candidate/signal payload, replay reuses it without
  rewriting artifacts or recording another ledger event.
- `max_reruns` (default 3, configurable 1–10) in `AssemblyReviewRunConfig`
  bounds the loop.
- Rerun failure raises `ResumeValidationError` without modifying prior
  candidate refs.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_assembly_review tests.test_review_resume tests.test_durable_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
