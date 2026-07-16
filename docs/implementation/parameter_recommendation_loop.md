# Parameter Recommendation Loop Implementation Guide

The authoritative boundary and DTO specification is
[`../architecture/parameter-recommenders.md`](../architecture/parameter-recommenders.md).

## Packages

Model-boundary code lives under:

```text
app/llm/parameters/assembly/
app/llm/parameters/hic/
```

Stage orchestration lives under:

```text
app/stages/assembly/parameters/
app/stages/hic_scaffolding/parameters/
```

Do not add new `parameter_*.py` files at the stage root. Put profile, basis,
catalog, workflow, and storage responsibilities in the stage's `parameters/`
package. Put request DTOs, prompt rendering, model invocation, and response
parsing in the matching `llm/parameters/<stage>/` package.

## Assembly lifecycle

1. `profile.py` builds the complete audit profile and a task-scoped view.
2. `basis.py` snapshots the selected method catalog and evidence.
3. `llm/parameters/assembly/request.py` constructs the strict request DTO.
4. `prompt.py` renders the exact messages; `workflow.py` persists them before
   invocation.
5. `recommender.py` invokes the model and `response.py` accepts only the exact
   `parameters` response contract.
6. `workflow.py` persists the raw response, parsed recommendation, and
   `response_parse.json`.
7. Parse success always opens the hash-bound human parameter-approval gate.
8. Approval creates a compact attempt cursor. Freeze reads the exact approved
   `parameters` mapping as `l2_recommended`.

There is no `parameter_validation.py`, sparse-patch merge, baseline/effective/
execution parameter trio, compatibility alias, or model-unavailable default.

## AutoHiC lifecycle

AutoHiC follows the same persistence and approval sequence with its own DTO and
one response parameter: `restriction_enzyme`. Any non-empty, config-safe string
that satisfies the response shape is shown to the reviewer. The parser does not
consult an enzyme catalog.

## Catalog and execution boundary

Assembly catalogs remain under:

```text
descriptors/knowledge_packs/assembly/methods/<method>/params.yaml
```

They describe the executor surface and the parameters shown to the model. The
request DTO exposes L2 metadata without catalog default values. Parser success
does not depend on catalog membership, level, enum, or numeric range.

After human approval, `build_parameter_sources()` separates runtime-owned L0,
observed L1, and the exact approved L2 mapping. `build_command()` checks whether
those sources can be rendered for the selected tool. This is deliberately later
than recommendation review and must never rewrite the recommendation.

## Failure semantics

- provider or JSON/shape failure: `parameter_response_invalid`, no approval,
  no attempt, no fallback;
- parsed response: mandatory human approval, regardless of biological value;
- human rejection: terminal rejection, no attempt;
- approved but unrenderable command input: explicit freeze/parameter error;
- execution or review failure: immutable retry context may produce a new LLM
  recommendation, subject to retry limits.
