# Phase 3 Natural Entry, Review Resume, and Real Execution

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> before implementing code from this plan. Steps use checkbox (`- [ ]`) syntax
> for implementation tracking. Documentation checkboxes are not evidence that
> code exists.

**Goal:** Make Gene Agent usable from natural language, make blocked human
review states resumable, then add real method execution one scoped slice at a
time.

**Architecture:** Phase 3 keeps the existing structured graph entry and blocked
review state as the source of truth:
`start / ask -> structured request -> initial_state(...) -> graph.invoke(...)
-> blocked_human -> inspect review request -> structured decision -> resume`.
LLM parsing stays outside LangGraph. Review/resume is a control-plane adapter
over `active_interrupt`, stage artifacts, ledger events, and checkpoint state;
it must not become an alternate router.

**Tech Stack:** Python 3.11, LangGraph `StateGraph`, Pydantic v2, PyYAML only
for existing descriptor/config roles, standard-library `unittest`,
OpenAI-compatible LangChain chat model profiles when LLM parsing is explicitly
enabled.

---

## Current Context

Phase 2 proved the full CycloneSEQ backbone contract and the first executable
method slice, `read_qc.long_read_stats.seqkit_stats`. The current usable entry
is structured:

```text
gene-agent run --session-id ... --manifest ... [--data-dir ...]
-> argparse.Namespace
-> initial_state(...)
-> build_graph().invoke(...)
```

`app/llm/` exists but is still placeholder-level. Review gates already set
`supervision_state: blocked_human` and `active_interrupt`, especially
`assembly_review` and `hic_scaffolding`, but there is no approved resume command
or resume payload schema.

## Interaction Loop

Phase 3 task 01 and task 02 are one interaction loop, not isolated features:

```text
start / ask
  -> run graph
  -> blocked_human
  -> inspect review request
  -> human decides
  -> resume
  -> continue graph
```

Task 01 owns how a workflow starts. It may let a user say "use
inputs/manifest.yaml to start session s1" or "start from /data/sample.fa", but
it must only produce a narrow `EntryRequest`. For source files or folders, the
adapter materializes the internal manifest first and then calls the same
structured `run --manifest ...` path.

Task 02 owns what happens after a workflow pauses. It must let a user inspect
the blocked state and review artifact, submit a structured decision, and resume
the same workflow without restarting from inventory.

## Identity And Progress Model

Use these identities consistently:

| Term | Meaning |
|---|---|
| `session_id` | Workflow run primary key. It answers "which workflow run is this?" |
| `thread_id` | LangGraph checkpoint identity. It normally matches `session_id` for one run. |
| `current_stage` | Current workflow location. |
| `stage status` | Stage-level state such as planned, completed, failed, or blocked. |
| `attempt_id` | One execution attempt for one stage/method. Multiple attempts may exist in one session. |
| `checkpoint` / `state_version` | Recoverable state snapshot at a point in time. |
| `artifact_ref` / `asset_ref` | Durable result references produced or registered by a stage/attempt. |
| `ledger` | Append-only audit stream for session, attempt, artifact, review, and lifecycle events. |

`session_id` alone is not enough to answer where the workflow is or whether a
stage should rerun. A status/inspect command should use `session_id` to load the
latest runtime state/checkpoint and ledger summary, then report
`current_stage`, `supervision_state`, `active_interrupt`, stage statuses, and
latest artifacts.

If the user says "continue s1", the system must load the latest checkpoint for
`session_id=s1` and resume from the blocked/current state. It must not create a
fresh `initial_state(...)` unless the user explicitly starts a new workflow.

## Design Constraints

- Keep the parent graph thin; do not add a natural-language parser node inside
  the graph.
- Keep `TopLevelState` small; store requests, review decisions, metrics, logs,
  and raw LLM responses as artifacts or ledger events when they need
  durability.
- Reuse the existing structured entry path instead of creating a second graph
  startup path.
- Use LLM parsing only to produce a validated structured request. The LLM must
  not mutate graph state, choose hidden defaults, write descriptors, or control
  resume directly.
- Separate lifecycle actions:
  - `resume` continues from a human gate after a validated decision.
  - `retry` retries a failed retryable stage/method attempt.
  - `rerun` creates a new attempt for an already completed or failed stage.
  - `rebuild-from` recomputes a stage and downstream dependents.
- Treat YAML sources narrowly:
  - `config/*.yaml` owns runtime defaults and manually approved policy knobs.
  - `descriptors/**/*.yaml` owns package/method/assets capability facts.
  - prompt bundles own LLM instructions.
  - request/session facts belong in structured request objects, artifacts, or
    ledger records, not new YAML files.
- Avoid duplicating method candidate lists between Python constants,
  `config/defaults.yaml`, and `descriptors/registry.yaml`. Prefer
  `descriptors/registry.yaml` / `by_stage` as the method catalog and use Python
  only for temporary task-local compatibility filters until a descriptor policy
  exists.
- Do not introduce new top-level stages for method-family roles that already
  belong inside the current parent graph. In particular, proposed roles named
  `assembly_qc`, `assembly_polish`, `gene_structure_annotation`, and
  `functional_annotation` should map into existing `assembly_review`,
  post-review actions, `gene_annotation`, and `function_annotation` unless a
  future graph-contract change is explicitly approved.

## Plan Files

- [01 Entry Interaction](01_natural_language_entry_parser.md)
- [02 Review Inspect And Resume Interaction](02_review_resume_semantics.md)
- [03 Stage Method Catalog And Planner Corrections](03_stage_method_catalog_and_planner_corrections.md)
- [04 Read QC Realization](04_read_qc_realization.md)
- [05 Genome Survey Realization](05_genome_survey_realization.md)
- [06 Assembly Realization](06_assembly_realization.md)
- [07 Assembly Review Realization](07_assembly_review_realization.md)
- [08 Polish And Purge Decision Actions](08_polish_purge_decision_actions.md)
- [09 Hi-C Scaffolding Realization](09_hic_scaffolding_realization.md)
- [10 Repeat, Gene, And Function Annotation Realization](10_annotation_realization.md)

## Future Task Direction

After tasks 01, 02, and 03 are implemented and verified, real execution should
follow the biological mainline from read QC onward instead of jumping to an
isolated tool slice. Each task should make one stage genuinely usable, then pass
stable refs to the next stage.

Do not broaden execution inside one task. Each stage realization should define
which method route is in scope, which alternatives remain candidates, what
artifacts are produced, and which decisions still require human approval.

## Recommended Task Order

1. Add the natural-language entry parser outside LangGraph.
2. Add validated review/resume semantics for existing blocked review gates.
3. Correct stage method catalog and planner gates before real execution.
4. Realize read QC: stats, FOFN, optional report, and optional approved
   filtering.
5. Realize genome survey only for valid short-read inputs; keep ONT/CLR-only
   non-blocking.
6. Realize the first ONT/CLR draft assembly route.
7. Realize assembly review evaluation before the human gate.
8. Realize post-review polish/purge/pass actions.
9. Realize optional Hi-C scaffolding when Hi-C data and chromosome-level intent
   exist.
10. Realize repeat, gene, and function annotation in downstream order.
11. Decide requested-stage routing only after natural entry and resume payloads
   are stable.
12. Decide `RemoteRunner` semantics separately; do not bundle it with natural
   entry, resume, or a local method slice.

## Phase 3 Done Criteria

- A natural-language request can be parsed into the same structured run contract
  used by `gene-agent run`.
- Structured callers can bypass LLM parsing.
- Intent parsing errors fail before graph invocation.
- A blocked `assembly_review` or `hic_scaffolding` state can resume from a
  validated decision payload.
- A user can inspect session progress by `session_id` and see whether the run is
  running, completed, failed, or blocked for human review.
- Review decisions are auditable through artifacts and ledger events.
- Stage method catalogs do not imply over-running filters, long-read k-mer
  survey, hifiasm for ONT/CLR, duplicated repeat-library discovery, or IsoQuant
  on genome long reads.
- QC and downstream stages progress from planning contracts to real method
  execution one stage at a time.
- Real method execution remains descriptor-backed:
  descriptor -> frozen attempt -> runner -> artifact records -> ledger events.
- No new YAML layer duplicates request facts, checkpoint state, review payloads,
  or method catalogs.

## Human Decision Rule

Stop and ask before choosing:

- natural-language command name and UX beyond the minimum `ask`/parser adapter;
- default session-id generation policy;
- review decision payload schema beyond task 02;
- durable checkpoint/session snapshot format beyond the minimum required for
  status and resume;
- semantics for `retry`, `rerun`, and `rebuild-from`;
- requested-stage routing semantics;
- method candidate priority rules not already in descriptors;
- new YAML source-of-truth roles;
- any real bioinformatics threshold or automatic accept/reject policy.

## Verification

Documentation-only:

```bash
git diff --check
```

Baseline after code changes:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
