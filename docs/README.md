# Documentation Map

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

## Authority entry points

| Concern | Owner |
|---|---|
| Documentation policy and change control | [`architecture/reference/governance.md`](architecture/reference/governance.md) |
| Architecture contracts and current implementation status | [`architecture/README.md`](architecture/README.md) |
| Python dependencies | [`../pyproject.toml`](../pyproject.toml) |
| Public CLI | [`../app/entrypoints/run.py`](../app/entrypoints/run.py) |
| Stage registration | [`../app/workflow.py`](../app/workflow.py) |
| Package and method capabilities | [`../descriptors/registry.yaml`](../descriptors/registry.yaml) |
| Active host mapping | `GENE_AGENT_ENVS_CONFIG`, otherwise ignored machine-local [`../config/envs.yaml`](../config/envs.yaml) |
| Missing-host seed | [`examples/envs.yaml.example`](examples/envs.yaml.example) |
| NCBI snapshot installation and file layout | [`../data/reference/ncbi/README.md`](../data/reference/ncbi/README.md) |
| Schema-valid operator inputs | [`examples/`](examples/) |
| Run and recovery procedure | [`user_guide/run-and-recovery.md`](user_guide/run-and-recovery.md) |
| New-host installation | [`deployment/migration.md`](deployment/migration.md) |
| Mac/server update procedure | [`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md) |

The architecture map routes each exact contract and status projection. Document
roles, synchronization rules, and quality gates are owned by governance.
