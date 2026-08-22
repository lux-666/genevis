# Documentation Map and Authority

The working tree contains one current owner for each contract. Git history, not
parallel Markdown files, retains superseded designs and implementation history.

## Choose the audience first

- [User Guide](user_guide/README.md) routes installation, host preparation,
  contig execution, gates, inspection, and recovery.
- [Developer Guide](developer_guide/README.md) routes code, tests, descriptors,
  architecture contracts, and implementation evidence by change type.
- [Architecture Map](architecture/README.md) remains the primary design entry
  for contributors; splitting the audience indexes does not move or weaken any
  architecture owner.

The two audience indexes are navigation only. They must link to the same
operator procedures and architecture contracts rather than restating them.

## Fastest reading paths

For an operator:

```text
README.md
  -> user_guide/README.md
     -> deployment/migration.md for a new host
     -> user_guide/run-and-recovery.md for run/inspect/review/resume
  -> examples/ for copyable inputs
```

For architecture or implementation work:

```text
docs/README.md
  -> developer_guide/README.md
     -> architecture/README.md
     -> topics/system-map.md
        -> one affected topic
           -> its linked reference owner
              -> langgraph-implementation-map.md for current wiring/evidence
```

Do not read `docs/architecture/` linearly. Topics explain how subsystems connect;
reference pages own exact fields, invariants, arrays, defaults, and lifecycle
semantics.

## Authority matrix

| Concern | Single authority | Non-authoritative consumers |
|---|---|---|
| Repository documentation hierarchy and change control | `architecture/reference/governance.md` | This router and README summaries |
| Python dependencies and optional dev tools | `../pyproject.toml` | Installation commands in README/deployment |
| Public CLI flags | `../app/entrypoints/run.py` | User/deployment command examples |
| Stage registration and executable recommendation helpers | `../app/workflow.py` | Planner/TUI callers and explanatory docs |
| Biological stage boundaries and canonical recommendation meaning | `architecture/reference/stage-contracts.md` | Topics, README, examples |
| Package/method identity and capabilities | `../descriptors/registry.yaml` plus referenced descriptors | Method tables, environment checks, implementation map |
| Active host environment/package mapping consumed by execution | `GENE_AGENT_ENVS_CONFIG` when set, otherwise ignored machine-local `../config/envs.yaml` | Bootstrap, dependency snapshot, server checks |
| Committed bootstrap seed for a missing host mapping | `../config/envs.yaml.example` | Bootstrap only; it is not a second active resolver input |
| Schema-valid operator inputs | `examples/` | README and operator guides link to them rather than copying JSON/YAML |
| Run/freeze/progress/checkpoint/Ledger/report/retention contract | `architecture/reference/data-contract.md` | Runtime/data topics and operator explanations |
| Method freeze/run/collect/settle contract | `architecture/reference/execution-lifecycle.md` | Execution topic and stage adapters |
| Process result/evidence/log contract | `architecture/reference/execution-evidence.md` | Execution topic and implementation map |
| Cross-stage logical resource handoff | `architecture/reference/resource-handoff.md` | Data/stage topics and implementation map |
| Parameter recommendation DTO/persistence/approval/retry contract | `architecture/reference/parameter-recommendation.md` | Parameter topic and operator guide |
| Human-facing run and recovery procedure | `user_guide/run-and-recovery.md` | README and deployment pages provide only short entry links |
| New-host installation | `deployment/migration.md` | README prerequisite summary |
| Mac/server Git update boundary | `deployment/mac_dev_server_pull.md` | README host-boundary summary |
| Current graph/method/status/test projection | `architecture/langgraph-implementation-map.md` | Planning and review; it does not redefine contracts |
| Accepted design versus implementation gaps | `architecture/reference/conformance.md` | Topics and implementation map link to it |

`app/control/projections.py` is the single executable workflow-progress reducer;
the exact semantic contract is
`architecture/reference/data-contract.md`. Checkpoint/marker/lock fields remain
execution diagnostics and must not be promoted to progress truth by a guide.

The public durable diagnostic report is the **session summary** stored as
`reports/session_summary.json` and `reports/session_summary.md`. Internal legacy
identifiers do not create a second external report kind.

## Document roles

| Location | Allowed content | Must not own |
|---|---|---|
| `../README.md` | Product scope, five-stage contig entry, shortest commands, links | Exact schemas, method registry, freeze/review/progress contracts |
| `user_guide/README.md` | Operator navigation by task | A second copy of installation, run, gate, or recovery procedures |
| `developer_guide/README.md` | Contributor navigation by change type | Architecture contracts, status projections, or implementation history |
| `architecture/topics/` | Task-oriented explanation, subsystem edges, small diagrams, links | Normative arrays, default values, exact DTO/schema/path contracts |
| `architecture/reference/` | Exact contracts, invariants, ownership, governance, conformance | Tutorial copies or historical progress logs |
| `architecture/langgraph-implementation-map.md` | Current wiring, implementation status, evidence, bounded debt projection | Intended behavior not backed by an owner contract |
| `user_guide/` | Operator-visible behavior and procedures | Exact internal schemas already owned by reference pages |
| `deployment/` | Host provisioning and Git/update workflow | A second run/freeze/recovery manual |
| `examples/` | Schema-valid copyable manifest/RunConfig inputs | Explanatory pseudo-config that cannot validate |

Legacy `superpowers/specs/` and `superpowers/plans/` material has been removed
from the working tree. Do not recreate or cite those paths as current owners.

## Change synchronization

For a behavioral change:

```text
direct user decision
  -> one owning reference contract
  -> code and focused tests
  -> affected explanatory topic(s)
  -> implementation map when wiring/status/evidence/debt changed
  -> operator/deployment guide only when the user procedure changed
  -> README/router only when product scope or navigation changed
```

For an operator-only wording change, update the single user/deployment owner and
its inbound links; do not restate the architecture contract elsewhere.

The implementation map is the only maintained cross-cutting current-status
projection. Do not add phase reports, historical completion percentages,
handoff snapshots, or another architecture summary to the working tree.

## Drift checks

Repository tests should keep at least these boundaries executable:

- every relative Markdown link resolves;
- README does not regrow an exact run-contract manual;
- deployment pages link to the operator guide instead of copying freeze/resume
  semantics;
- planner recommendations derive from `app/workflow.py` rather than hand-written
  parallel arrays;
- server readiness derives its method closure from the authoritative example and
  descriptor/environment data rather than a shell-script tool list.
