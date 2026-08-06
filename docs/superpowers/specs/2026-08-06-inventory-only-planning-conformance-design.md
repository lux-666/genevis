# Inventory-Only Planning And Documentation Conformance Design

## Status

Approved by the user on 2026-08-06. This specification closes the mismatch
between the valid `RunConfig.requested_stages=[]` contract and the Ask/TUI
interfaces, then synchronizes public and architecture documentation with the
occurrence-based runtime model.

## Problem

The structured RunConfig schema accepts an empty requested-stage list as a
valid Inventory-only run. The interactive planning stack does not:

- `AskClarificationResponse` treats an empty stage-selection list as a missing
  payload;
- TUI Plan Draft validation requires a non-empty list;
- the stage prompt says Enter accepts the recommendation, but an empty
  recommendation produces an invalid response.

The documentation simultaneously claims that `[]` is valid and presents the
fixed stage registry order as if it were the runtime workflow. The architecture
status and implementation map therefore overstate conformance.

## Goals

- Represent three distinct planning states correctly:
  - `None`: stage selection has not been answered;
  - `[]`: the operator explicitly selected an Inventory-only run;
  - non-empty list: the exact requested occurrence sequence.
- Let Ask, plain-text TUI, questionary TUI, and LLM Plan Draft paths create and
  confirm an empty occurrence sequence.
- Preserve the exact empty list through RunConfig construction and whole-run
  freeze.
- Correct README and architecture wording so registry/default recommendation
  order is not described as runtime successor order.
- Use occurrence terminology for local-process authorization.

## Non-Goals

- No change to the implicit `step-000-inventory` runtime occurrence.
- No automatic insertion, ordering, or normalization of biological stages.
- No change to method or parameter selection when biological occurrences exist.
- No broader TUI midstage-intake implementation.
- No dynamic stage-plugin registry.

## Planning State Semantics

`AskPlanState.selected_stage_occurrences` remains optional because an unfinished
draft needs a missing state. Validation and routing must distinguish identity
from truthiness:

```text
selected_stage_occurrences is None -> ask stage-selection question
selected_stage_occurrences == []   -> selection complete; skip method/parameter questions
selected_stage_occurrences != []   -> process selected occurrences normally
```

`AskClarificationResponse(kind="stage_selection")` requires that
`selected_stage_occurrences` is not `None`; it does not require a non-empty
list. Other clarification-kind validation remains unchanged.

Merging an empty selection clears stale method and parameter choices exactly as
any other stage-selection replacement does. `build_run_config_payload` writes
`"requested_stages": []`, which remains validated by the strict RunConfig
schema.

## Interactive And LLM Draft Behavior

The stage-selection prompt keeps the empty recommendation visible as an
intentional option. Its text must state that Enter accepts the recommendation,
including an Inventory-only recommendation, rather than implying that at least
one stage is required.

When the recommendation is empty and the operator presses Enter:

```text
stage selection -> []
  -> no occurrence-local execution-consent prompts
  -> no method selection
  -> no parameter selection
  -> final confirmation
  -> RunConfig.requested_stages=[]
```

The Plan Draft parser requires a JSON list, not a non-empty list. Unknown-stage
and explicit-intent-sequence checks still apply to non-empty content. An empty
explicit intent may be preserved as an empty draft.

The final plan summary must render the absence of biological occurrences as
`Inventory only` or equivalent operator-facing wording, not as an unfinished or
invalid plan.

## Runtime Semantics

No runtime code special case is introduced. Existing
`workflow_steps_from_run_config` prepends Inventory, producing:

```json
[
  {
    "step_id": "step-000-inventory",
    "stage": "inventory",
    "allow_local_execution": false
  }
]
```

Inventory commits, advances to the terminal cursor, and the route controller
records session completion. There is no synthetic skip event for absent
biological stages.

## Documentation And Architecture Status

The following current-tree drift must be corrected:

- the top-level README `Workflow` arrow chain is renamed to a stage registry or
  default recommendation and explicitly says it is not a successor table;
- README local-execution language uses `occurrence`, because authorization is
  not shared by repeated occurrences of the same stage;
- `topics/tui-and-planning.md` explicitly documents interactive empty-plan
  creation, not only structured RunConfig validity;
- `topics/runtime-orchestration.md` retains literal occurrence-sequence
  authority and Inventory-only terminal behavior;
- `langgraph-implementation-map.md` names focused tests for the empty interactive
  path and does not mark it complete before those tests exist;
- `reference/conformance.md` records the gap while it exists and removes the
  entry in the same implementation change that closes it.

The docs continue to distinguish:

- `STAGE_ORDER`: unique registration/display/default-recommendation order;
- requested occurrences: exact mutable pre-freeze plan;
- `workflow_steps`: immutable runtime routing authority.

## Error Handling

- Missing `selected_stage_occurrences` remains invalid for a stage-selection
  response.
- A non-list Plan Draft payload remains invalid.
- Unknown stage names remain invalid.
- A non-empty response that changes an explicit occurrence sequence remains
  invalid.
- Empty method or parameter payload validation is unchanged because those
  clarification kinds are not reached for an Inventory-only plan.

## Verification

Focused tests must prove:

- `AskClarificationResponse` accepts `selected_stage_occurrences=[]` and rejects
  `None` for stage selection;
- plain-text stage selection accepts Enter with an empty recommendation;
- questionary stage selection returns the same empty response;
- Plan Draft parsing accepts an empty list and rejects missing/non-list values;
- merging the empty selection clears stale method and parameter state;
- `next_clarification` advances directly to confirmation;
- confirmation builds a schema-valid RunConfig with `requested_stages=[]`;
- the frozen executor creates only `step-000-inventory` and completes normally;
- README and architecture searches no longer present the fixed registry chain as
  runtime successor order.

After focused tests, run formatting, lint, typing, and the complete test suite.

## Acceptance Criteria

1. Every interactive planning surface can deliberately create an Inventory-only
   run.
2. `None` and `[]` retain distinct semantics throughout planning.
3. The empty sequence freezes and executes without a special runtime branch.
4. Current documentation no longer confuses registry order, recommendation
   order, and runtime occurrence order.
