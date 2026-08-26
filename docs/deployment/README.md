# Deployment Entry Points

This page is the deployment boundary map. It keeps the three host-level
operations distinct; it does not duplicate the run, freeze, review, or resume
contract owned by [`user_guide/run-and-recovery.md`](../user_guide/run-and-recovery.md).

## One authority per operation

| Operation | Authoritative entry | Owns | Does not own |
|---|---|---|---|
| Bootstrap | `gene-agent bootstrap` / `bootstrap --update` | machine configuration lifecycle, the active environment mapping, declared package canaries, and every method entry declared in that mapping | package installation, biological inputs, or a run session |
| Contig acceptance | [`scripts/server_check.sh`](../../scripts/server_check.sh) | read-only orchestrator prerequisites, fixed-stage method/package closure, and host BUSCO/NCBI database integrity derived from the authoritative five-stage contig template | deferred Assembly-candidate readiness, LLM reachability, private inputs, or biological execution |
| Code verification | [`scripts/server_test.sh`](../../scripts/server_test.sh) | pytest regression, compileall, and diff check | Conda tool readiness, real-data smoke, or any biological command |

## Host configuration lifecycle

`docs/examples/envs.yaml.example` is the generic seed used only when the active host
mapping is missing. The default active mapping is `config/envs.yaml`; that file
is machine-local deployment state and must remain ignored/untracked. All public
readiness paths select the same mapping: an explicit `bootstrap --config PATH`
wins for that bootstrap invocation, otherwise `GENE_AGENT_ENVS_CONFIG` wins,
otherwise the repository default is used. Use an absolute override when commands
may start outside the checkout; a relative override is resolved from the caller's
current directory, while the default path is anchored to the checkout.

The default bootstrap command collects missing values, previews and validates
one plan—including declared package-conformance actions—then waits for
confirmation before applying that same plan. `--update` selects existing
bootstrap-owned fields for the same preview/confirmation flow.
Bootstrap then checks every package and method entry declared in the active
mapping, including any Assembly candidates the host exposes. That catalog scope
may be broader than the fixed methods checked for one contig run.

Bootstrap does not install Conda environments, tools, databases, BUSCO lineages,
models, or an LLM service. Those are provisioned by the operator and referenced
by the active mapping or private environment configuration.

Commands assume the documented `gene_agent` orchestrator environment was
activated once for the current shell. That name is an installation convention,
not a value resolved from the host mapping; tool environments are resolved from
the mapping.

```bash
gene-agent bootstrap
```

## Verification sequence

Run code verification immediately after installing and activating the
orchestrator environment. Run contig acceptance after provisioning the host:

```bash
bash /path/to/gene_agent/scripts/server_test.sh
bash /path/to/gene_agent/scripts/server_check.sh
```

`server_check.sh` derives its fixed-stage method closure through the same Python
readiness code used by `gene-agent run`; it deliberately does not maintain a
shell list of tools. Assembly method selection remains deferred until upstream
evidence is available. The selected candidate is resolved and checked from the
frozen dependency snapshot at Assembly stage entry. Because the authoritative
contig template includes Assembly Review, the check also validates the BUSCO
lineage and NCBI snapshot declared in the active host mapping. The actual run
additionally validates its manifest and selected LLM endpoint.

A successful bootstrap is not a proof that `server_check.sh` will pass, and a
successful `server_check.sh` is not a proof that every declaration in the active
mapping is healthy. Their scopes differ, so run both on the checkout that will
execute the work. If bootstrap used `--config PATH`, export that same absolute
path as `GENE_AGENT_ENVS_CONFIG` for `server_check.sh` and the actual run.

`server_test.sh` is a code-level regression gate. It runs pytest, compileall,
and `git diff --check`; it is not a real-data or biological-tool smoke test.
The still-open full-path acceptance boundary is recorded only in
[Architecture Conformance](../architecture/reference/conformance.md#five-stage-real-tool-contig-acceptance-is-not-automated).

For new-host installation see [`migration.md`](migration.md). For the Mac/Git
handoff see [`mac_dev_server_pull.md`](mac_dev_server_pull.md). For starting or
recovering a session see [`Run and Recovery`](../user_guide/run-and-recovery.md).
