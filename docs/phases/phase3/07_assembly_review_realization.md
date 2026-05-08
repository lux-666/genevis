# 07 Assembly Review Realization

Task status: planned.

## Goal

Run real evaluation on `draft_assembly` before the human review gate.

This task absorbs the proposed `assembly_qc` role into the existing
`assembly_review` stage. Do not add a new top-level `assembly_qc` stage unless
the parent graph contract is explicitly changed later.

## Scope

Start with one or two concrete evaluation methods, preferably QUAST and then
BUSCO if the environment and fixtures are manageable.

Merqury remains conditional on short DNA reads. Mapping coverage, purge checks,
polishing, and contamination screening can follow in later tasks.

Candidate evaluation methods:

- `seqkit_assembly_stats`
- `quast_basic`
- `busco_genome`
- `merqury_qv`
- `blobtoolkit_contamination`

Decision artifacts/method roles:

- `candidate_rank_bundle`
- `assembly_review_request`
- `human_assembly_decision`
- `promote_final_assembly`

## Boundaries

- Do not auto-accept or reject assemblies from metrics.
- Do not invent QUAST, BUSCO, Merqury, or coverage thresholds.
- Do not run polish or purge before the human review decision.
- Do not discard rejected candidate assemblies.
- Do not let an LLM pick the final assembly. LLMs may summarize, rank, explain
  risk, or prepare a review bundle; final promotion is a human decision.

## Expected Outputs

- `assembly_stats`
- `busco_report`
- `merqury_report`
- `blobtoolkit_report`
- `assembly_qc_bundle`
- `assembly_eval_report`
- updated review signals with available/failed status
- review request artifact for human decision

## Acceptance

- Real evaluation artifacts are registered and traceable.
- The review gate still blocks when policy or thresholds are not approved.
- Human decision remains required before final assembly promotion for generated
  candidates.
- Review display includes candidate id, assembler, N50, total size, contig
  count, BUSCO, QV, duplication, contamination signal, and warnings when those
  signals are available.
