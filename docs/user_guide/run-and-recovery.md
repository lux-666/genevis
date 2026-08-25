# Run and Recovery

This is the single operator guide for starting, inspecting, reviewing, and
resuming a Gene Agent session. Behavioral explanations live in
[Runtime orchestration](../architecture/topics/runtime-orchestration.md) and
[Review and resume](../architecture/topics/review-and-resume.md); exact lifecycle
semantics are owned by the
[Run data contract](../architecture/reference/data-contract.md) and its linked
reference contracts. CLI flags are defined by `app/entrypoints/run.py`;
deployment pages link here instead of copying the run contract.

## Before a real run

Provision a Linux/server checkout with
[New Host Setup](../deployment/migration.md), then run:

```bash
bash scripts/server_check.sh
bash scripts/server_test.sh
```

These checks have deliberately different scope:

- `server_check.sh` probes orchestrator prerequisites, then derives and probes
  the fixed-stage tool/package closure in
  `docs/examples/contig_run_config.template.json` through the active environment
  mapping. Assembly is deliberately deferred and its selected candidate is
  checked at stage entry. It also validates the host BUSCO/NCBI database
  registry needed by Assembly Review. The script does not validate real input
  files or the LLM endpoint.
- `server_test.sh` runs the Python regression/compile/diff checks. It is not a
  real-data bioinformatics smoke test.
- `gene-agent run` performs run-specific input, fixed tool/package, database,
  and LLM checks before it publishes frozen inputs; a deferred Assembly method
  is checked later from that frozen dependency snapshot.

## Five-stage contig input

Use the paired templates:

```bash
cp docs/examples/contig_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/contig_run_config.template.json /server/path/run_config.json
```

This pair is the authoritative five-stage contig example named in the root
README; the exact occurrence list remains in the JSON rather than being copied
into another guide. Inventory is added as the implicit first occurrence during
freeze. Fill every placeholder with server-local paths. The template
intentionally leaves the Assembly method and parameters unset. After upstream
QC/survey evidence exists, Assembly recommends and confirms a compatible method,
then recommends its parameters. The active environment mapping must declare the
tool requirements of every candidate that the run may select.

The example files, not this guide, own the complete schema-valid payload. The
exact occurrence and freeze contract is linked above; RunConfig remains the
literal occurrence/authorization authority. Deferred Assembly method and
parameter decisions are stage-owned evidence rather than startup configuration.

## Start

Launch a new run from a real interactive terminal:

```bash
gene-agent run \
  --manifest /server/path/manifest.yaml \
  --run-config /server/path/run_config.json \
  --data-dir /server/path/data
```

The manifest owns `session_id`. Output is stored below:

```text
/server/path/data/runs/<session_id>/
```

Both stdin and stdout must be TTYs before freeze. A selected occurrence may
start a local process only when its frozen local-execution authorization allows
it. `review_policy.max_blocked_seconds` applies to every registered review gate,
including both Assembly gates and Assembly Review; `review_policy.stages` is
only an optional per-stage override. Exact default and countdown semantics are
owned by the
[Review Contract](../architecture/reference/data-contract.md#12-review-contract).
Parameter and review gates follow the frozen policy and registered resolver:
depending on that policy, a gate may wait for a human or resolve through its
eligible attached-process countdown path. An interrupted durable gate remains
manual recovery work; a later process does not reconstruct the old countdown.

The optional conversational entry is:

```bash
gene-agent tui --data-dir /server/path/data
```

TUI and structured `run` converge on the same frozen-session executor.

## Inspect

Human-readable inspection:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data
```

After canonical completion, `final_assembly` is the verified absolute path of
the latest assembly delivered by the workflow. The JSON form also includes its
immutable `primary_output_ref`, candidate bundle, producer stage, and workflow
occurrence; failed integrity checks are reported instead of returning a path.

Machine-readable polling for one or many independent sessions:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --json
```

`workflow_progress` is derived from the verified frozen plan, canonical Ledger
terminal facts, and a verified active gate. Checkpoint, execution marker, and
lock state belong to the adjacent `execution` diagnostics; they are not progress
truth. If frozen inputs cannot be verified, `inspect` withholds plan-derived
fields instead of guessing a recovery authority.

Generate the diagnostic session summary only when needed:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --trace
```

`--trace` is a report-generation operation, not a polling protocol. Its public
output is the **session summary** under `reports/session_summary.json` and
`reports/session_summary.md`. For diagnosis it may build a limited view from
occurrence-bearing Ledger/artifact evidence when the frozen plan is unavailable;
that fallback does not authorize control or resume. Internal compatibility names
such as `write_final_review_report` or `latest_final_report` do not define a
second external report type.

## Review interactively

For a durable human gate:

```bash
gene-agent review \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --checkpointer sqlite
```

The command validates the active request and writes the decision evidence before
applying the resume transaction.

## Resume from a decision file

First generate a request-bound template:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --write-decision-template /server/path/decision.json
```

Edit only the decision fields exposed by that template, then resume:

```bash
gene-agent resume \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --decision-file /server/path/decision.json \
  --checkpointer sqlite
```

Resume verifies session identity, occurrence/gate binding, immutable evidence,
decision shape, and checkpoint availability before changing workflow state.

## Upgrade safety

The current pre-release runtime has no checkpoint migration. Before pulling new
code over a checkout with active sessions, either finish those sessions or keep
the old checkout and matching environment until they are terminal. A report is
not a checkpoint and cannot substitute for durable resume state.
