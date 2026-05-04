# 06 Assembly Review Gate

Task status: implemented.

## Goal

Make `assembly_review` the main human gate for accepting, rerunning, purging, or
stopping an assembly candidate.

## Files

- Modify: `app/stages/assembly_review/nodes.py`
- Modify: `app/stages/assembly_review/routing.py`
- Modify: `app/review/` if a shared review module exists
- Modify: `app/schemas/artifacts.py`
- Modify: `app/control/ledger.py`
- Modify: `app/schemas/plan.py`
- Add: `descriptors/packages/{quast,busco,merqury,minimap2,purge_dups}.yaml`
- Add: `descriptors/methods/assembly_review/`
- Modify: `descriptors/registry.yaml`
- Test: `tests/test_assembly_review.py`

## Project Context

`assembly_review` already matters in Phase 1 because existing assembly input can
route directly here. This task must preserve that route while adding candidate
review contracts for assemblies produced by the `assembly` stage.

This is the main human gate in the genome workflow.

## Node Plan

Replace the placeholder with:

```text
enter_assembly_review
-> resolve_candidate_assemblies
-> plan_review_metrics
-> collect_or_reference_review_signals
-> evaluate_review_rules
-> human_review_gate
-> commit_review_decision
```

Suggested nodes:

- `resolve_candidate_assemblies`: accept existing assembly input or candidate
  assembly refs.
- `plan_review_metrics`: plan QUAST, BUSCO, Merqury, mapping coverage,
  purge_dups_check, contamination_check when inputs permit.
- `evaluate_review_rules`: structural checks only until thresholds are approved.
- `human_review_gate`: set `blocked_human` when accept/rerun/purge/continue
  cannot be deterministic.
- `commit_review_decision`: publish `final_assembly_for_scaffolding_or_annotation`
  only after explicit decision or deterministic safe path.

## Reuse From `rna_agent`

Reusable patterns:

- human gate as explicit review/control node;
- semantic conflict payload shape with compact failed rules and metadata;
- ledger event for review requested/decision recorded;
- rejected candidate traceability.

Do not copy:

- cell type semantic review;
- annotation label-map semantics.

## Optimization Notes

- Keep threshold values out of code until human-approved.
- Separate metric collection from decision interpretation.
- Preserve all candidate refs; do not overwrite losing assemblies.

## Input Contract

Required:

- one or more `draft_assembly` candidate bundle refs

Optional:

- `clean_long_reads`
- `clean_short_dna_reads`
- `genome_survey_result`
- lineage/species hints

## Review Signals

Candidates:

- QUAST
- BUSCO
- Merqury when short reads exist
- read mapping coverage
- purge_dups_check
- contamination_check

Review decision options:

- accept candidate;
- rerun assembly;
- purge;
- continue with low-confidence note;
- stop for human review.

## Tasks

- [x] Add assembly evaluation bundle schema.
- [x] Add review decision artifact schema.
- [x] Add descriptor placeholders for QUAST, BUSCO, Merqury, mapping coverage,
  purge_dups_check, and contamination_check.
- [x] Add blocked-human state for unresolved assembly review decisions.
- [x] Add tests that candidate disagreements do not auto-advance without policy.
- [x] Add tests that review output keeps rejected candidates traceable.
- [x] Add ledger event shape for review requested and review decision recorded.

## Implemented State

- `app/stages/assembly_review/builder.py` now builds a real deterministic review
  gate instead of the generic placeholder.
- `app/stages/assembly_review/nodes.py` resolves existing assembly registry
  inputs and the `assembly` stage's `selected_candidate_refs` bundle JSON files.
- Candidate refs, rejected candidate reasons, planned signals, review request
  payloads, and deterministic decisions are kept traceable.
- `AssemblyEvalBundle` and `ReviewDecisionArtifact` are defined in
  `app/schemas/artifacts.py`.
- `AssemblyReviewPlan` is defined in `app/schemas/plan.py`.
- `app/review/base.py` defines shared `ReviewSignal`, `ReviewPayload`, and
  review decision literals.
- `app/control/ledger.py` records `review_requested` and `review_decision`
  events.
- QUAST and BUSCO are always planned as pending signals; Merqury is planned only
  when `clean_short_dna_reads` exists; mapping coverage and purge_dups checks are
  planned when `clean_long_reads` exists; contamination check remains a planned
  pending signal.
- The stage writes `assembly_eval_bundle.json`, `assembly_review_request.json`,
  and `assembly_review_decision.json` artifacts as appropriate.
- Existing assembly input can auto-accept deterministically and publish
  `final_assembly_ref`; assembly-stage candidates always block for human review
  until an approved resume payload exists.
- No metric thresholds are encoded.
- `tests/test_assembly_review.py` covers existing assembly auto-accept, 05
  candidate bundle consumption, blocked-human review gates, registry-driven
  signal planning, rejected candidate traceability, ledger events, and review
  artifacts.

## Human Decisions Required

Stop and ask before choosing:

- BUSCO completeness thresholds;
- BUSCO duplication thresholds;
- QUAST/N50 accept thresholds;
- Merqury QV/completeness thresholds;
- purge_dups cutoffs;
- contamination policy;
- exact human approval payload schema.

## Non-Goals

- Do not implement automatic purge or polish in Phase 2.
- Do not auto-accept assemblies from one metric.
- Do not implement resume behavior until the approval payload is approved.
- Do not discard rejected candidates.

## Acceptance Criteria

- Assembly review can collect or reference evaluation signals.
- Human-gated decisions are explicit.
- Candidate accept/reject/rerun decisions are traceable.
- No threshold is invented by coding assistants.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly_review tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
