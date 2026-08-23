# Recommendation Policy Contract

> **Status:** implemented for Assembly method/route and Assembly parameter
> advisors.
> **Read when:** changing ablation policy, deterministic baselines, advisor
> preflight, or recommendation evidence.  
> **Comes from:** operator draft policy or structured RunConfig.  
> **Feeds into:** [Run data](data-contract.md), [stage contracts](stage-contracts.md),
> [parameter recommendation](parameter-recommendation.md), and
> [TUI and planning](../topics/tui-and-planning.md).  
> **Code evidence:** `app/schemas/tui_intake.py`, `app/schemas/run_config.py`,
> `app/entrypoints/ask_planner.py`, `app/entrypoints/run.py`,
> `app/stages/assembly/nodes.py`, and
> `app/stages/assembly/parameters/workflow.py`.

## Definition

`RecommendationPolicy` owns two independent, frozen advisory modes:

```json
{
  "method_route_advisor": "advisor",
  "parameter_advisor": "advisor"
}
```

Each value is either `"advisor"` or `"deterministic_baseline"`. The modes are
independent so an experiment can ablate one advisory boundary without changing
the other. There is no generic enabled boolean and no policy that can bypass a
human gate.

## Draft and freeze boundary

A mutable TUI draft carries `recommendation_policy` before planning. It is
projected through `entry_request.recommendation_policy` into the planner and
compiled into `RunConfig.assembly` at whole-run freeze. The frozen fields are
`method_route_advisor` and `parameter_advisor`; the draft policy cannot be
changed after freeze.

The policy never reintroduces an Assembly method or parameter into RunConfig.
Method identity remains stage-owned. A policy selects who proposes the method
or parameter mapping, not an executable answer.

## Method/route advisor

`advisor` is the normal transaction: after QC/survey evidence, the exact model
request, raw response, parsed recommendation, and human confirmation are
persisted before `assembly_selection` binds.

`deterministic_baseline` skips the model call. It requires an explicit route,
a normalized shared read type, or a single registered input platform. The
default method for that read type is selected only when it is in the
descriptor-backed compatible set. Mixed inputs without enough deterministic
evidence fail closed. The resulting selection is written as a policy decision
with `method_recommendation_mode="deterministic_baseline"` and does not open
the method-advisor human gate.

## Parameter advisor

`advisor` is the schema-3 parameter recommendation transaction: immutable
request, raw response, sparse mapping, approval gate, and approved attempt.

`deterministic_baseline` builds an empty-L2 schema-3 baseline from the same
validated facts, catalog, context, and dependency refs. It records a policy
summary, creates an approved initial attempt without model request/response
evidence, and lets execution resolve L0/L1 values only. Execution adapters use
the baseline resolver and must not apply an L2 mapping in this mode. Retry is
not supported by the initial baseline policy; a retry request fails closed
rather than silently switching to advisor mode.

## Readiness and evidence

Readiness follows the frozen policy. A method/route baseline does not require
the Assembly method LLM endpoint; an advisor run does. A parameter baseline
does not invoke the parameter model. The Assembly cursor persists both policy
modes, and downstream evidence records the decision source rather than relying
on absent artifacts.
