# Deployment Entry Points

This page is the deployment boundary map. It keeps host preparation and
run-evidence acceptance distinct; it does not duplicate the run, freeze, review, or resume
contract owned by [`user_guide/run-and-recovery.md`](../user_guide/run-and-recovery.md).

## One authority per operation

| Operation | Authoritative entry | Owns | Does not own |
|---|---|---|---|
| Bootstrap | optional `gene-agent bootstrap` / `bootstrap --update` assistant | generating, extending, and validating the active environment mapping, declared package canaries, and every method entry declared in that mapping | exclusive authorship of the mapping, package installation, biological inputs, or a run session |
| Host readiness | [`scripts/server_check.sh`](../../scripts/server_check.sh) | read-only orchestrator prerequisites, fixed-stage method/package closure, and host databases from an explicit RunConfig; defaults to the five-stage contig template | deferred Assembly-candidate readiness, LLM reachability, private inputs, or biological execution |
| Code verification | [`scripts/server_test.sh`](../../scripts/server_test.sh) | pytest regression, compileall, and diff check; current conditional real-tool isolation caveat below | host readiness or proof of real-data E2E acceptance |
| Complete HiFi route evidence | `python -m app.diagnostics.route_acceptance --run-dir PATH` | read-only process/collection evidence, handoffs, request-bound paired final evaluation and final output | running tools, changing decisions, independent QV, or guaranteed biological improvement |

## Host configuration lifecycle

`docs/examples/envs.yaml.example` is the generic seed used only when the active host
mapping is missing. The default active mapping is `config/envs.yaml`; that file
is machine-local deployment state and must remain ignored/untracked. All public
readiness paths select the same mapping: an explicit `bootstrap --config PATH`
wins for that bootstrap invocation, otherwise `GENE_AGENT_ENVS_CONFIG` wins,
otherwise the repository default is used. Use an absolute override when commands
may start outside the checkout; a relative override is resolved from the caller's
current directory, while the default path is anchored to the checkout.

Operators may create and edit the active mapping and project `.env` directly;
bootstrap is optional. Manual and assisted edits use the same runtime format,
and either route may be followed by bootstrap validation.

The default bootstrap command collects the core Conda target and asks whether
to configure the optional Assembly Review database and AutoHiC groups. A group
may be skipped until its resources are installed; bootstrap still previews and
publishes the valid remaining plan. Rerun the ordinary command later to add a
missing group, or edit the YAML manually. `--update` replaces selected existing
managed fields through the same preview/confirmation flow.

Bootstrap checks every package, method, and database entry declared in the
active mapping, including any Assembly candidates the host exposes. An omitted
optional group is not a bootstrap error. `server_check.sh` or confirmed-run
readiness still fails closed when its workflow requires that group.

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

This command is an assisted configuration route, not a mandatory predecessor
to execution. A manually prepared active mapping may proceed directly to the
appropriate readiness check.

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
lineage and NCBI snapshots under the root declared in the active host mapping.
The actual run additionally validates its manifest and selected LLM endpoint.
For the complete HiFi route, pass the edited RunConfig as the script's first
argument. Templates, execution and post-run evidence checks are owned by
[Run and Recovery](../user_guide/run-and-recovery.md#complete-hifihic-server-acceptance).

A successful bootstrap is not a proof that `server_check.sh` will pass, and a
successful `server_check.sh` is not a proof that every declaration in the active
mapping is healthy. Their scopes differ, so run both on the checkout that will
execute the work. If bootstrap used `--config PATH`, export that same absolute
path as `GENE_AGENT_ENVS_CONFIG` for `server_check.sh` and the actual run.

`server_test.sh` is a code-level regression gate. It runs pytest, compileall,
and `git diff --check`; it is not a real-data or biological-tool smoke test
harness. The suite can auto-run SeqKit when its test environment is present;
strict tool-free selection is pending. Follow the
[staged acceptance order](../user_guide/run-and-recovery.md#acceptance-order-code-tests-before-real-end-to-end-runs)
before a full-route run: code tests, small tool smokes, contig E2E, independent
Hi-C/finishing slices, then complete real E2E.
The still-open full-path acceptance boundary is recorded only in
[Architecture Conformance](../architecture/reference/conformance.md#five-stage-real-tool-contig-acceptance-is-not-automated).

For new-host installation see [`migration.md`](migration.md). For the Mac/Git
handoff see [`mac_dev_server_pull.md`](mac_dev_server_pull.md). For starting or
recovering a session see [`Run and Recovery`](../user_guide/run-and-recovery.md).
