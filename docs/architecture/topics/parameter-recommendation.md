# Parameter Recommendation

> **Status:** implemented for Assembly, AutoHiC, and T2T.
> **Contracts:** [parameter recommendation](../reference/parameter-recommendation.md),
> [review](../reference/data-contract.md#12-review-contract), and
> [execution lifecycle](../reference/execution-lifecycle.md).
> **Code evidence:** `app/domains/parameter_recommendation/`,
> `app/llm/parameters/`, `app/storage/parameter_evidence.py`, and
> `app/stages/*/parameters/workflow.py`.

A recommender runs after method selection. Assembly waits for its confirmed
`assembly_selection`; fixed-method stages already know their method. Canonical
facts and the selected method produce a stage-specific request, immutable model
evidence, a sparse recommendation, an approval request, and finally the approved
`ParameterAttempt` consumed by execution.

Assembly, AutoHiC, and T2T share evidence, approval, and attempt ownership but
retain stage-specific biological inputs:

- Assembly supplies route, read type, target level, and method guidance.
- AutoHiC supplies species facts, Hi-C context, restriction-enzyme evidence,
  and its adjustable catalog.
- T2T supplies species facts, resolved telomere motif, long-read context, and
  its threshold catalog.

Exact DTO fields, sparse-response validation, persistence, approval timing,
retry semantics, and command-resolution precedence remain in the linked
reference contracts.
