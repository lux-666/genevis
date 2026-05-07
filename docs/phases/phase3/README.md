# Phase 3 Natural Entry, Review Resume, and Real Execution

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> before implementing code from this plan. Steps use checkbox (`- [ ]`) syntax
> for implementation tracking. Documentation checkboxes are not evidence that
> code exists.

**Goal:** Make Gene Agent usable from natural language, make blocked human
review states resumable, then add real method execution one scoped slice at a
time.

**Architecture:** Phase 3 keeps the existing structured graph entry as the
source of truth:
`CLI / natural language adapter -> structured request -> initial_state(...) ->
build_graph().invoke(...)`. LLM parsing stays outside LangGraph. Review/resume
is a control-plane adapter over `active_interrupt`, stage artifacts, ledger
events, and checkpoint state; it must not become an alternate router.

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

## Plan Files

- [01 Natural-Language Entry Parser](01_natural_language_entry_parser.md)
- [02 Review Resume Semantics](02_review_resume_semantics.md)
- [03 Stage Method Catalog And Planner Corrections](03_stage_method_catalog_and_planner_corrections.md)

## Future Task Direction

After tasks 01, 02, and 03 are implemented and verified, scope real execution
slices one method at a time. The first good candidate is a tiny-fixture
`assembly_review.eval.quast` execution slice because it exercises descriptor
loading, frozen attempt generation, local runner execution, artifact collection,
review signal update, and ledger events without touching the whole assembly or
annotation catalog.

Do not start broad real execution until the entry and resume surfaces are
stable and the method catalog distinguishes defaults, conditional candidates,
helpers, and non-default compatibility routes. Each method task should specify
the exact descriptor, input fixture, runner mode, output artifacts, ledger
events, and review handoff before code is changed.

## Recommended Task Order

1. Add the natural-language entry parser outside LangGraph.
2. Add validated review/resume semantics for existing blocked review gates.
3. Correct stage method catalog and planner gates before real execution.
4. Add one tiny real execution slice after entry/resume/catalog gates are in
   place.
5. Decide requested-stage routing only after natural entry and resume payloads
   are stable.
6. Decide `RemoteRunner` semantics separately; do not bundle it with natural
   entry, resume, or a local method slice.

## Phase 3 Done Criteria

- A natural-language request can be parsed into the same structured run contract
  used by `gene-agent run`.
- Structured callers can bypass LLM parsing.
- Intent parsing errors fail before graph invocation.
- A blocked `assembly_review` or `hic_scaffolding` state can resume from a
  validated decision payload.
- Review decisions are auditable through artifacts and ledger events.
- Stage method catalogs do not imply over-running filters, long-read k-mer
  survey, hifiasm for ONT/CLR, duplicated repeat-library discovery, or IsoQuant
  on genome long reads.
- Real method execution remains descriptor-backed:
  descriptor -> frozen attempt -> runner -> artifact records -> ledger events.
- No new YAML layer duplicates request facts, checkpoint state, review payloads,
  or method catalogs.

## Human Decision Rule

Stop and ask before choosing:

- natural-language command name and UX beyond the minimum `ask`/parser adapter;
- default session-id generation policy;
- review decision payload schema beyond task 02;
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
