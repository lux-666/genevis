# Parameter Recommendation Closed Loop

This document is a methodology summary. The normative module, DTO, response,
and persistence boundaries are in
[`../architecture/parameter-recommenders.md`](../architecture/parameter-recommenders.md).

The current loop is intentionally human-supervised:

```text
upstream biological observations
  -> stage-specific LLM request DTO
  -> exact `parameters` JSON response
  -> response-shape parsing only
  -> human review
  -> approved parameters frozen for execution
  -> execution/review evidence for an optional retry
```

Catalog descriptions, method guidance, and literature cases help the model
reason. They do not become application-owned biological rules after the model
responds. The application does not merge sparse patches, fill omitted model
values from catalog defaults, or reject a parsed recommendation because a value
is unknown or outside a suggested range.

Every recommendation persists its audit profile, immutable basis, exact model
request, raw response, parsed recommendation, response-parse result, and sealed
manifest. Human approval is bound to the manifest hash. A malformed response or
unavailable model produces no fabricated recommendation and no execution
attempt.

Retries use the immediately preceding sealed attempt and its execution or
review-failure evidence. The LLM again returns an explicit parameter mapping;
the application does not construct the retry by merging a delta with the prior
attempt.
