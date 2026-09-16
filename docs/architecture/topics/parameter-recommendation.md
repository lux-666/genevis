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

Assembly's case consumer uses one source and shared retrieval
for method choice and parameter recommendation. Broad material matches, including
other assemblers, inform method choice first. Once the method is confirmed,
direct and cross-method cases plus software/parameter explanations inform its
sparse parameter mapping. Reference-tool settings retain their source identity.

The [Assembly case contract](../reference/casepack.md) owns the implemented
exchange and cutover; [conformance](../reference/conformance.md#assembly-software-explanation-coverage)
records remaining software-guidance coverage. Existing approval/execution
boundaries remain unchanged. Hi-C and T2T literature/method extensions are deferred.
