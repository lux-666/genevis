# Developer Guide Index

This page is a navigation layer for contributors working on Gene Agent code,
architecture, descriptors, tests, or documentation. It does not define runtime
contracts. Exact rules belong to the linked architecture reference owners.

## Start here

- [Architecture map](../architecture/README.md) — topic router, reference
  registry, status vocabulary, and documentation synchronization rules.
- [System map](../architecture/topics/system-map.md) — module ownership and the
  three graph scales.
- [Governance](../architecture/reference/governance.md) — source-of-truth,
  change control, boundary rules, and repository quality policy.
- [Implementation map](../architecture/langgraph-implementation-map.md) — the
  current graph/method wiring, verification evidence, and bounded debt. It is a
  status projection, not a replacement for a contract.
- [Conformance register](../architecture/reference/conformance.md) — accepted
  design versus current-tree gaps.

## Route by change

| Change area | Read next |
|---|---|
| Parent routing, frozen occurrences, terminal behavior | [Runtime orchestration](../architecture/topics/runtime-orchestration.md), then [Run data contract](../architecture/reference/data-contract.md) |
| Stage inputs, outputs, methods, or biological boundaries | [Stage contracts](../architecture/reference/stage-contracts.md), then the affected stage implementation under `app/stages/` |
| Method freeze, runner, collection, or retry | [Method execution](../architecture/topics/execution.md), [Execution lifecycle](../architecture/reference/execution-lifecycle.md), and [Execution evidence](../architecture/reference/execution-evidence.md) |
| Cross-stage files, resource resolution, or assembly delivery | [Resource handoff](../architecture/reference/resource-handoff.md) |
| LLM parameters, approval, or retry chains | [Parameter recommendation](../architecture/reference/parameter-recommendation.md) and the corresponding stage parameter workflow |
| Human gates, review, or resume | [Review and resume](../architecture/topics/review-and-resume.md), then the run-data and execution references it links |
| Package/method registration or environment capability | [Descriptor contract](../architecture/reference/descriptors.md), `descriptors/registry.yaml`, and the referenced descriptor files |
| Bootstrap or host configuration | [Project bootstrap](../architecture/topics/bootstrap.md), then committed `config/envs.yaml.example`, ignored active `config/envs.yaml`, and the execution resolver |

## Tests and contribution entry

- [Test suite](../../tests/) — focused boundary tests and full regression
  coverage. Start with the test file named by the implementation-map row you
  are changing.
- [Project scaffold tests](../../tests/test_project_scaffold.py) — structural
  checks for stage order, storage boundaries, architecture roots, and removal
  of retired planning tracks.
- [Project instructions](../../AGENTS.md) — repository-local scope and safety
  rules.
- [Python/development metadata](../../pyproject.toml) — dependency and tool
  configuration owner.
- [Server verification helpers](../../scripts/) — host checks and regression
  checks; deployment behavior is routed from the user guide below.

Before a non-trivial behavior change, identify one owning reference contract,
update focused tests with the code, then update affected topics and the
implementation map only when wiring, status, evidence, or debt changed. Do not
add a second architecture summary, phase report, or handoff snapshot.
