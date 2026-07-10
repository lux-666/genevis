# Task 4: Hifiasm Methods-Aware Evidence Recommendation

**Phase**: 11
**Status**: MVP implemented; richer case-assessment response and artifact split pending
**Depends on**: Task 2 EvidenceCase contract; Task 3 numeric retrieval
**Created**: 2026-07-10
**Updated**: 2026-07-10

## Goal

Let the hifiasm parameter recommender use the manually curated Methods evidence
from numerically plausible cases, while keeping numeric matching, source
provenance, parameter selection, and deterministic validation distinguishable
and auditable.

## Implemented MVP

- hifiasm receives `evidence_cases` with numeric trace and exact source excerpts,
  while legacy `selected_cases` remains empty for this method;
- no-case payloads remain valid and explicitly fall back to readonly L1 facts,
  SKILL heuristics, retry context, and catalog defaults;
- the prompt publishes the L0/L1/L2 permission contract and allowed evidence
  bases;
- optional `parameter_evidence` is validated when present, including visible
  profile fact paths and case/span citations;
- catalog/rule validation still rejects any L0/L1 output or invalid L2 value;
- compact `ParameterAttempt` can retain the optional parameter evidence map.

The MVP uses sequential hybrid retrieval:

```text
hard compatibility
  -> continuous numeric shortlist
  -> LLM Methods/mechanism assessment inside the existing decision boundary
  -> hifiasm parameter recommendation
  -> deterministic catalog/rule validation
```

A separate embedding service, vector database, and rank-fusion infrastructure
are deliberately deferred. The corpus is expected to be small enough for the
numeric shortlist and referenced Methods excerpts to fit the controlled prompt.

## Prompt Evidence Assembly

Only Task 3 numeric candidates may contribute Methods evidence.

For each candidate, the prompt receives:

- case ID and source metadata;
- numeric-distance and evidence-coverage breakdown;
- observed facts relevant to the query;
- source-reported hifiasm version and parameters;
- approved rationale claims;
- selected exact source excerpts referenced by span ID;
- reported outcomes and their provenance;
- an explicit list of missing fields.

The prompt does not receive the full article body by default. It receives only
validated excerpts required by the case. Full `body.md` and `methods.md` remain
available through artifact/source refs for audit.

## One Decision Call, Two Explicit Sections

The first release should extend the existing assembly decision response rather
than introduce a second uncoordinated model client. The response contains two
separate sections:

```yaml
case_assessments:
  - case_id: case_a
    relevance: strong
    transferable_findings:
      - effect_area: purging
        direction: decrease_aggressiveness
        evidence_span_ids: [methods.parameter_rationale]
        explanation: "..."
    conflicts_or_limits:
      - "The case has substantially higher coverage."
    accepted_as_support: true

recommendation:
  adjustable_params:
    purge_level:
      action: set
      value: 1
      supporting_case_ids: [case_a]
      supporting_span_ids: [methods.parameter_rationale]
      confidence: medium
      reason: "..."
    kmer_length:
      action: keep_default
      value: 51
      supporting_case_ids: []
      supporting_span_ids: []
      confidence: low
      reason: "No sufficiently specific evidence supports changing the catalog default."

selected_case_ids: [case_a]
summary: "..."
```

This keeps mechanism assessment visible without paying for an additional LLM
round trip. If later evaluation shows that joint assessment and recommendation
bias each other, the same schema can be split into two decision calls without
changing the EvidenceCase or numeric-retrieval contracts.

## Hifiasm Effect Areas

Task 4 uses a small hifiasm-specific reasoning vocabulary:

```text
kmer_and_seed_selection
overlap_filtering
error_correction
repeat_handling
graph_cleaning
haplotype_preservation
purging
performance_and_memory
```

This vocabulary organizes Methods claims; it is not yet a cross-tool ontology
and does not map hifiasm flags to Flye or NextDenovo flags.

A parameter recommendation may cite an effect area only when:

- the EvidenceCase has an approved rationale claim or reported observation for
  that effect;
- the cited span exists in the selected case;
- the target parameter exists as hifiasm L2 in the catalog;
- the catalog description or curated parameter-effect metadata associates the
  parameter with that effect area.

## Parameter-Level And Evidence-Basis Rule

Every hifiasm L2 parameter remains visible to the LLM, preserving the Phase10
catalog contract. Visibility does not imply that every parameter should be set.
L0 remains owned by executor/run configuration, and L1 remains readonly profile
context. Neither cases nor the LLM may alter L0 or L1 values.

For every L2 parameter, the model chooses one action:

- `set`: evidence supports a non-default or explicit value;
- `keep_default`: evidence is weak, absent, conflicting, or not specific enough;
- `omit`: allowed only for parameters whose catalog rendering treats omission
  as the authoritative default behavior.

Each `set`, `keep_default`, or `omit` decision records one or more evidence
bases:

```text
case
profile_fact
skill_heuristic
retry_failure
catalog_default
```

Case evidence is optional. A decision using `basis: case` must cite at least one
selected case and one validated span from that case. A decision based on L1
profile facts cites fact paths; a SKILL heuristic cites the relevant heuristic
label; a retry recommendation cites the failure/review signal; and an unchanged
default may use `catalog_default`. The validator rejects false or out-of-scope
citations, but it does not reject a recommendation merely because no case was
available.

## Evidence Strength

The model does not invent a universal scientific score. It assigns an ordinal
support label based on visible facts:

- `strong`: numeric context is close, rationale is explicit, source parameter
  is reported, and outcome is available;
- `medium`: evidence is relevant but one important element is missing or the
  recommendation requires a limited inference;
- `weak`: evidence is indirect, incomplete, conflicting, or distant.

Weak evidence should make the recommendation conservative, but it is not a
hard-coded ban on every non-default value. A strong L1 fact, an applicable
SKILL heuristic, or a retry-specific failure signal may still support a bounded
L2 change without a paper case. Catalog and hard-rule validation remain the
execution gate.

## Prompt Contract Changes

`build_prompt_payload` adds ref-backed sections for hifiasm V2:

```yaml
case_retrieval:
  backend: hifiasm_numeric_v2
  query_ref: .../retrieval_query.json
  result_ref: .../numeric_retrieval.json
  candidate_case_ids: []
  warning: null

evidence_cases:
  - case_id:
    numeric_match: {}
    observed_facts: {}
    intervention: {}
    rationale_claims: []
    source_excerpts: []
    outcomes: {}
```

The old `selected_cases` payload remains the contract for methods still using
V1. Hifiasm uses one V2 payload path after cutover; it must not receive both V1
and V2 case representations.

## Validation

Deterministic post-LLM validation must verify:

- all assessed case IDs were in the numeric shortlist;
- all selected case IDs were accepted in `case_assessments`;
- all cited span IDs belong to the cited case;
- every parameter names an allowed evidence basis;
- case-backed parameters cite only shortlisted case IDs and visible span IDs;
- profile-fact-backed parameters cite readonly prompt facts rather than writing
  them as output parameters;
- every parameter exists and is L2 in the hifiasm catalog;
- values satisfy catalog types, ranges, allowed values, and hard-rule shards;
- the response does not cite body/Methods content that was absent from the
  prompt;
- unsupported effect areas or free-form parameter names are rejected.

Recommendation validation remains separate from evidence relevance. A case can
be relevant while a proposed value is invalid.

## Attempt Artifacts

Each hifiasm attempt writes:

```text
data/artifacts/assembly/<session>/parameter_attempt_<n>/
├── input_profile.json
├── retrieval_query.json
├── numeric_retrieval.json
├── evidence_payload.json
├── prompt_payload.json
├── llm_response.json
└── command.json
```

Compact `parameter_attempts` state stores refs and summaries:

```yaml
case_retrieval_backend: hifiasm_numeric_v2
candidate_case_ids: []
selected_case_ids: []
retrieval_result_ref: ...
evidence_payload_ref: ...
evidence_warning: null
```

## Error Handling

- No eligible cases: send an empty evidence set with a conservative warning and
  continue recommendation from readonly L1 facts, SKILL guidance, retry context,
  and catalog defaults.
- Weak evidence coverage: include cases for explanation, lower the evidence
  strength, and prefer defaults; do not block a bounded L2 recommendation that
  has another allowed evidence basis.
- Invalid case/span citation in LLM output: validation failure before freeze.
- Model unavailable: preserve the Task1 strict production behavior; do not
  silently pretend that Methods evidence was assessed.
- Source checksum changes after validation: fail knowledge-pack loading before
  recommendation.

## Tests

Add tests for:

- only numeric-shortlisted cases enter the evidence payload;
- exact source excerpts, not full documents, enter the prompt;
- unsupported case and span citations fail validation;
- case-backed parameter changes require valid case/span citations;
- no-case recommendation can still use L1 facts and SKILL heuristics;
- weak/no case evidence prefers defaults without making case retrieval a hard
  prerequisite;
- every cataloged hifiasm L2 parameter remains visible;
- invalid values still fail existing parameter validation;
- selected case IDs and artifact refs are persisted on the typed attempt;
- V1 and V2 case payloads are not both used for hifiasm;
- model-unavailable behavior remains strict.

## Acceptance Criteria

- [ ] Hifiasm recommendation consumes a numeric shortlist plus validated Methods
  excerpts.
- [ ] LLM output separates case assessment from parameter recommendation.
- [ ] Every value records an allowed evidence basis; case-backed values are
  traceable to selected case IDs and source span IDs.
- [ ] Missing cases do not block recommendation, and weak evidence causes more
  conservative use of L1/SKILL/default guidance.
- [ ] Existing catalog/rule validation remains the execution gate.
- [ ] Full source documents remain ref-backed and outside compact state.
- [ ] No embedding service, vector database, GraphRAG, LLM Wiki, or cross-tool
  adapter is introduced.
