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
> `app/llm/json_response.py`, `app/stages/assembly/decision.py`,
> `app/llm/parameters/assembly/recommender.py`,
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
the other. There is no generic enabled boolean.

A policy may replace the *proposal source* for the two named advisors. It does
not bypass the method/route human confirmation gate in advisor mode, and it does
not bypass parameter approval in advisor mode. A deterministic parameter
baseline records its own immutable policy decision and approved attempt; that is
a distinct policy path, not an approval-gate bypass.

## Persisted advisor-response envelope

New Assembly route and parameter-advisor writes share the envelope keys
`schema_version`, `raw_response`, `validity`, `error`, and `model_id`.
`app/llm/json_response.py` owns construction and validation of that common wire
shape, including `validity` values `parsed` and `invalid`. It does not own
version-specific replay compatibility.

The route restorer retains schema-v1 migration behavior: an absent or
non-string historical `model_id` is normalized to `null`. The Assembly
parameter restorer for schema v2 rejects a non-string `model_id`. Changing
either rule requires an explicit schema/migration decision; a shared helper
must not silently make the two restore policies identical. New writes always
emit a string or `null` in both schemas.

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
summary, persists the exact request it would have sent plus explicit
policy-owned response and parse records, and creates a hash-bound approved
initial attempt with an automatic policy decision through the same schema-3
manifest boundary. The decision metadata uses `source="policy"` and a complete
automatic-resolution audit, so it is distinguishable from a foreground
countdown. Execution adapters use the baseline resolver and must not apply an
L2 mapping in this mode. Retry is not supported by the initial baseline policy;
a retry request fails closed rather than silently switching to advisor mode.

## Readiness and evidence

Readiness follows the frozen policy. When both Assembly advisors use baselines,
the run no longer requires the Assembly LLM endpoint before freeze; any Hi-C or
T2T advisor still does. The exact structured endpoint probe and no-session-on-
failure behavior are owned by the
[execution lifecycle](execution-lifecycle.md#whole-run-readiness-boundary). The
Assembly cursor persists both policy modes, and the
parameter baseline marks every skipped-model record explicitly rather than
relying on absent artifacts.

In advisor mode, the method-advisor request payload carries an explicit
`evidence_status` map. Fields paired with `"unavailable"` upstream evidence are
null and must not be inferred or treated as observations; the prompt rule
requires `insufficient_data` unless available evidence supports one route. The
model cannot recover a masked field by guessing, and a response that names a
method without sufficient route/read-type compatibility fails closed at
validation. See
[Run data](data-contract.md#101-assembly-method-recommendation) for the durable
placement of that status.
