# Hifiasm initial retrieval evaluation

The offline runner compares the legacy initial selector with the hifiasm
numeric initial retriever on the same version-controlled initial queries. It is
deterministic, network-free, and observational only.

The curated fixture uses the EvidenceCase v3 methods-only source contract:
each case declares a pack-root-relative `methods_path` plus bounded
`evidence_spans`. It has no `body.md`, `source_documents`, or source-file SHA.
`applicability.read_type` and `applicability.target_methods` remain case
attributes available to scoring; the evaluation runner does not turn them into
a separate discrete-routing layer.

Evaluation is not an activation or forward-progress gate. A report cannot
enable or disable `initial_cutover_enabled`, reject a shortlist, stop a
recommendation, or authorize execution. Corpus size, query count, scores,
coverage, scientific assessment, recorded recommendations, and other reported
metrics have no controlling threshold. The former 20-case and 10-query targets
are not production requirements.

## Run the fixture

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n gene_agent python \
  -m app.domains.assembly.hifiasm_evaluation \
  --corpus tests/fixtures/phase11/hifiasm_evaluation/corpus.yaml \
  --knowledge-root tests/fixtures/phase11/hifiasm_evaluation/knowledge_root/assembly \
  --output /tmp/hifiasm-evaluation.json \
  -k 5
```

The output declares:

```json
{
  "observational_only": true,
  "activation_effect": "none"
}
```

It records hashes for the corpus, strategy, initial case sets, and normalized
queries so the report can be replayed. Per-query output includes ordered
selected IDs, retrieval trace, Recall@k, nDCG@k, reciprocal rank, evidence
coverage, incompatible-case observations, unknown IDs, and holdout
observations. These values are for inspection only.

The corpus accepts only `recommend_initial` queries. Same-run retry never enters
an EvidenceCase selector, so there is no hifiasm retry corpus, query, evaluation,
database, shard, retriever, or selector backend.

## Production initial behavior

The descriptor switch is the complete activation setting:

```yaml
hifiasm_numeric:
  initial_cutover_enabled: true
```

When enabled, production ranks every eligible initial case with the Task 3
unified score: target-method match plus read-type match plus missing-aware
numeric similarity. Method and read type are scoring attributes rather than hard
filters. Score ties use evidence coverage, numeric distance, clade, and case ID.

It then takes the first five. If only one to four cases are eligible, all are
retained; with none, the existing no-case behavior is used. Caller limits and
evaluation results do not alter the production shortlist.

## Same-run retry behavior

After execution or review failure, retry uses attempt 0's immutable
profile/evidence/policy snapshot, the immediately preceding attempt, and its
structured failure facts. It does not retrieve historical retry cases, create a
candidate EvidenceCase, or promote runtime history into the knowledge pack.

Execution and review retries have separate counters and limits. Execution
failure with remaining execution budget does not pass through a failure
classifier or repairability gate; it proceeds to recommendation and automatic
validation. Review retry remains owned by the review stage and uses its own
directional diagnosis and budget.

## Human parameter approval

Initial and retry candidates share the same gate after automatic recommendation
and parameter validation:

```yaml
gate_id: parameter_approval
decisions_allowed: [approve_parameters, reject_parameters]
```

`approve_parameters` freezes the exact validated candidate and routes it to
execution. `reject_parameters` terminates without execution; for a retry it
also appends no attempt and consumes no execution or review retry budget. TUI
presentation is not part of Phase 11; future UI code may consume this generic
human-review contract.

## Focused verification

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n gene_agent python -m pytest \
  tests/test_hifiasm_numeric_retrieval.py \
  tests/test_hifiasm_offline_evaluation.py \
  tests/test_phase11_parameter_approval.py -q
```
