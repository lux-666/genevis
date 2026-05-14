# 07 Assembly Review Realization

Task status: implemented.

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

## Tasks

- [x] Keep `assembly_review` as the existing review/evaluation stage; no top-level `assembly_qc` stage is added.
- [x] Add one explicit local evaluation route for materialized draft assemblies: QUAST.
- [x] Keep default review behavior planning-only unless `stage_outputs.assembly_review.execution_mode == "local"` is explicitly set.
- [x] Register QUAST report artifacts, command stdout/stderr, updated evaluation bundle, and ledger attempt events.
- [x] Update candidate-specific QUAST review signals to `available`, `failed`, or `missing` without applying thresholds.
- [x] Preserve the human review gate for generated assembly candidates even when QUAST metrics are available.
- [x] Keep BUSCO, Merqury, mapping coverage, purge checks, contamination screening, polishing, and purging deferred. In-agent remote execution is intentionally unsupported (see `docs/deployment/mac_dev_server_pull.md`).

## Implemented State

- `app/stages/assembly_review/nodes.py` now keeps default metric planning as pending-signal contract behavior unless `stage_outputs.assembly_review.execution_mode == "local"` is explicitly set.
- Local QUAST execution validates the existing `assembly_review.eval.quast` descriptor, freezes deterministic `quast.py <assembly> -o <out_dir> --threads <threads>` attempts for candidates with materialized `final_assembly_ref` FASTA files, runs them through `LocalRunner`, and records ledger attempt events.
- QUAST success registers reference-free basic `report.tsv`, optional `report.txt`, command stdout/stderr, and a versioned `assembly_eval_bundle.evaluating.json` so previous artifact index hashes do not point at overwritten content. Parsed metrics such as contig count, total length, largest contig, N50, L50, and GC percent are stored as non-decisional signal metadata.
- QUAST failure registers command stdout/stderr artifacts, marks the candidate-specific QUAST signal as `failed`, and still routes to human review; the candidate is not rejected from metrics.
- Missing draft FASTA paths mark the QUAST signal as `missing` and do not start a local attempt.
- Existing assembly deterministic pass-through and generated-candidate human blocking semantics are preserved.
- `tests/test_assembly_review.py` covers descriptor loading, default non-execution, fake-QUAST success, fake-QUAST failure, missing draft FASTA behavior, review request payload signals, artifact registration, ledger events, and no-threshold policy.

改动要点：

- `app/stages/assembly_review/builder.py` and `routing.py` insert the optional QUAST execution branch between metric planning and structural review-rule evaluation.
- `app/schemas/artifacts.py` defines artifact constants for assembly review evaluation bundles and QUAST report files.
- Human review payload signals are candidate-specific, matching the evaluation bundle and reducing UI ambiguity.
- `descriptors/methods/assembly_review/quast_eval.yaml` and `descriptors/packages/quast.yaml` explicitly describe the current reference-free QUAST basic execution path.
- BUSCO remains a planned/pending signal in this task; no BUSCO databases, lineage choices, downloads, or execution policy are introduced.

## Acceptance

- Real evaluation artifacts are registered and traceable.
- The review gate still blocks when policy or thresholds are not approved.
- Human decision remains required before final assembly promotion for generated
  candidates.
- Review display includes candidate id, assembler, N50, total size, contig
  count, BUSCO, QV, duplication, contamination signal, and warnings when those
  signals are available.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly_review -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_assembly_review tests.test_review_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_assembly_review tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
