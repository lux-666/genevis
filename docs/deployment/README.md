# Deployment Entry Points

This page is the deployment boundary map. It keeps the three host-level
operations distinct; it does not duplicate the run, freeze, review, or resume
contract owned by [`user_guide/run-and-recovery.md`](../user_guide/run-and-recovery.md).

## One authority per operation

| Operation | Authoritative entry | Owns | Does not own |
|---|---|---|---|
| Bootstrap | `gene-agent bootstrap` / `bootstrap --apply` | machine configuration lifecycle, the active environment mapping, declared package canaries, and every method entry declared in that mapping | package installation, biological inputs, or a run session |
| Contig acceptance | [`scripts/server_check.sh`](../../scripts/server_check.sh) | read-only orchestrator prerequisites plus the selected method/package closure derived from the authoritative five-stage contig template | LLM reachability, private inputs, BUSCO lineage contents, or biological execution |
| Code verification | [`scripts/server_test.sh`](../../scripts/server_test.sh) | pytest regression, compileall, and diff check | Conda tool readiness, real-data smoke, or any biological command |

## Host configuration lifecycle

`config/envs.yaml.example` is the generic seed used only when the active host
mapping is missing. The default active mapping is `config/envs.yaml`; that file
is machine-local deployment state and must remain ignored/untracked. All public
readiness paths select the same mapping: an explicit `bootstrap --config PATH`
wins for that bootstrap invocation, otherwise `GENE_AGENT_ENVS_CONFIG` wins,
otherwise the repository default is used. Use an absolute override when commands
may start outside the checkout.

The default bootstrap command previews and validates a plan; `--apply` writes
it. Bootstrap then checks every package and method entry declared in the active
mapping, which may be broader than the methods selected by the current contig
template.

Bootstrap does not install Conda environments, tools, databases, BUSCO lineages,
models, or an LLM service. Those are provisioned by the operator and referenced
by the active mapping or private environment configuration.

```bash
conda run -n gene_agent gene-agent bootstrap
conda run -n gene_agent gene-agent bootstrap --apply
```

## Verification sequence

Run the scripts from any current directory; each resolves the repository root
from its own path:

```bash
bash /path/to/gene_agent/scripts/server_check.sh
bash /path/to/gene_agent/scripts/server_test.sh
```

`server_check.sh` derives its method closure through the same Python readiness
code used by `gene-agent run`; it deliberately does not maintain a shell list
of tools. A successful check is orchestrator and selected tool/package
acceptance only. The actual run
still validates its manifest, selected LLM endpoint, and BUSCO lineage before
freeze.

A successful bootstrap is not a proof that `server_check.sh` will pass, and a
successful `server_check.sh` is not a proof that every declaration in the active
mapping is healthy. Their scopes differ, so run both on the checkout that will
execute the work. If bootstrap used `--config PATH`, export that same absolute
path as `GENE_AGENT_ENVS_CONFIG` for `server_check.sh` and the actual run.

`server_test.sh` is a code-level regression gate. It runs pytest, compileall,
and `git diff --check`; it is not a real-data or biological-tool smoke test.

For new-host installation see [`migration.md`](migration.md). For the Mac/Git
handoff see [`mac_dev_server_pull.md`](mac_dev_server_pull.md). For starting or
recovering a session see [`Run and Recovery`](../user_guide/run-and-recovery.md).
