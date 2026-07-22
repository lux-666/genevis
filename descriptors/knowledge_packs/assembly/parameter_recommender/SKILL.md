# Assembly Parameter Recommendation

## Contract

Recommend an explicit parameter mapping for Flye, NextDenovo, hifiasm, or hifiasm-ont. L0 execution mechanics and measured/provided biological facts are context, not model-adjustable values. The recommendation is not merged with catalog defaults or a previous attempt and is not judged by application-owned biological hard rules; it is presented to a human for approval.

The model receives four distinct inputs:

1. Current-sample observations from read QC, genome survey, and run configuration.
2. The model-adjustable parameter catalog, without an application-selected baseline.
3. Optional paper case cards containing original parameters, excerpts, rationale, citation, applicability, outcomes, and numeric similarity.
4. Method-guidance cards describing biological mechanisms without fixed trigger values.

Paper cards are analogs, not recipes. Method, read type, route, assembly goal, and clade are not retrieval gates or score terms. The model judges transferability from each complete card. A novel organism does not need a matching paper case: mechanistic extrapolation from its biological observations is a normal recommendation mode.

## Selection, Augmentation, Generation, Validation

- Selection ranks paper cards by missing-aware numeric similarity across genome size, coverage, read N50, heterozygosity, and ploidy. Actionability only breaks ties. Missing dimensions are excluded and weights are renormalized.
- Augmentation presents each selected card exactly once and adds applicable method-guidance cards. It never synthesizes a second copy of parameters, quotes, rationale, or citation.
- Generation is one LLM call. The model judges the evidence and returns the explicit L2 parameter mapping it recommends passing for this attempt. It may omit an L2 parameter when the tool default should apply.
- Application parsing checks only the declared JSON response shape. Parameter values are shown to the human reviewer without catalog-range or biological hard-rule rejection.

## Biological Reasoning

Coverage and read length affect graph connectivity. Low coverage or short reads may favor retaining connectivity; deep coverage and long reads can support stricter overlap or graph-cleaning choices when their tradeoffs are acceptable.

Heterozygosity and ploidy affect purging. Aggressive purging can erase genuine haplotypic divergence. On retry, inflated assembly size and duplicated BUSCO can support stronger duplicate handling, while missing BUSCO, low size ratio, or collapsed haplotypes can support less aggressive handling.

Repeat fraction and genome size affect graph complexity and resource pressure. Treat these as interacting biological and computational signals rather than independent numeric triggers.

## Retry

Retry context is same-run evidence, never a literature case. The previous recommendation is context only. It is not a baseline and is not merged into the new recommendation.

For missing input, binary, permission, environment, or invalid-wrapper failures, prefer `expert_review` or `stop` instead of inventing a biological parameter change. For memory, timeout, parsing, or review-quality failures, return the new attempt's explicit parameter mapping independently; the application does not inherit omitted parameters from the previous attempt.

## Response

Return `parameters` and optionally `summary`. `parameters` is the exact mapping shown to the reviewer and consumed by command construction after approval. Do not return compatibility aliases, rich per-parameter wrappers, or copied evidence IDs. On retry also return `retry_disposition` (`retry`, `stop`, or `expert_review`).
