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
```

These checks have deliberately different scope:

- `server_check.sh` probes orchestrator prerequisites, then derives and probes
  the fixed-stage tool/package closure in
  `docs/examples/contig_run_config.template.json` by default (or the RunConfig
  supplied as its first argument) through the active environment
  mapping. Assembly is deliberately deferred and its selected candidate is
  checked at stage entry. It also validates the host BUSCO/NCBI database
  registry needed by Assembly Review. The script does not validate real input
  files or the LLM endpoint.
- `server_test.sh` is the earlier code-only gate run immediately after installing
  the orchestrator; New Host Setup owns that command and its position.
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

## HiFi gap closing from an existing assembly

Use [the manifest](../examples/gapcloser_manifest.template.yaml) and
[the RunConfig](../examples/gapcloser_run_config.template.json) for one complete
GapCloser artifact without rerunning Assembly, Hi-C, or T2T:

```bash
cp docs/examples/gapcloser_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/gapcloser_run_config.template.json /server/path/run_config.json
```

Replace the assembly/read paths and session ID. Supply the complete current
HiFi read set. The active `config/envs.yaml` must map
`gapcloser.tgs_gapcloser` to a tool environment with TGS-GapCloser 1.2.1,
minimap2, and GNU getopt (not the macOS BSD command). Use the ordinary `run`
command below. This slice needs no LLM endpoint and no evaluation databases.
Upstream's PB thresholds are exposed as operator settings, not claimed to be
optimal HiFi recommendations.

The tool emits a candidate FASTA, native segment details, and an indexed
validation report. The report measures gap/length changes and copied-segment
consistency, not QV or biological improvement. Use `review` or a request-bound
decision file to `accept_candidate`, `keep_prior_assembly`, or
`stop_human_review`. There is no automatic adoption; `inspect` reports the
final adopted/reused assembly after completion. If there are no internal
N-gaps, the stage reuses the input without invoking the tool.

ONT/CLR/CycloneSEQ/unknown or mixed-platform read sets are unsupported here.
The complete-workflow recommendation is not an input-compatibility guarantee;
omit unsupported occurrences explicitly. Base Polish also currently requires HiFi.
For independent quality evaluation, explicitly append an Assembly Review
occurrence and configure its separate tools, databases, and species metadata.
Exact scope and handoff semantics live in the
[GapCloser contract](../architecture/reference/stage-contracts.md#8-gapcloser).

An opt-in real-tool synthetic smoke test is available on a provisioned host.
It freezes the example configuration, runs Inventory and GapCloser, closes the
SQLite checkpoint, then exercises CLI resume and final `inspect` delivery.
Conda must be available, and the environment running pytest must expose the
TGS-GapCloser/minimap2/GNU getopt binaries (the test freezes `sys.prefix`):

```bash
GENE_AGENT_TGS_REAL_SMOKE=1 pytest -q tests/test_gapcloser.py -k real_tgs
```

This deterministic synthetic gap is not representative biological acceptance.

## Complete HiFi/Hi-C server acceptance

Copy [the full manifest](../examples/hifi_complete_manifest.template.yaml) and
[RunConfig](../examples/hifi_complete_run_config.template.json) to a server-local
directory. This is the complete recommended route, with final Review after
Base Polish. Edit every path, sample identity, genome-size estimate and species.
Supply the entire current HiFi read set and explicit Hi-C R1/R2 pairs. Do not
relabel ONT/CLR/CycloneSEQ reads as HiFi.

The template waits for manual decisions (`max_blocked_seconds: -1`). Before
running, set `assembly_dedup.automatic_purge_approved` to `true` only if the real
sample and desired haplotype representation justify purging; otherwise Dedup
retains the source and acceptance reports its execution coverage as incomplete.
If known, supply the actual Hi-C enzyme in
`hic_scaffolding.autohic_parameters.restriction_enzyme` and actual ploidy/telomere
motif in `shared_parameters.species_facts`; otherwise use the existing approved
resolver gates. Do not guess biological facts to remove a pause. Assembly still
selects its method/parameters at its stage gates; `assembly.case_source` may
point to the server's active exported v5 case directory.

Provision the configured method environments, AutoHiC assets, and the host
BUSCO/NCBI registry via the deployment guide. Base Polish adds Racon (1.5.x) and
minimap2; GapCloser requires TGS-GapCloser and GNU getopt. Then:

```bash
bash scripts/server_check.sh /server/path/run_config.json
gene-agent run --manifest /server/path/manifest.yaml \
  --run-config /server/path/run_config.json --data-dir /server/path/data
```

Review candidate evidence at each gate. Base Polish executes one Racon round
between two identically configured minimap2 alignments, preserves the source,
and requires `accept_candidate`, `keep_prior_assembly`, or `stop_human_review`.
The following Review evaluates both candidates with the same QUAST/BUSCO
settings, regardless of the earlier choice; its human decision chooses the
final assembly. Optional reference-dependent QUAST errors are unavailable
without a declared reference; read agreement is in-sample, never independent QV.

After the run, use the read-only acceptance entry point:

```bash
python -m app.diagnostics.route_acceptance \
  --run-dir /server/path/data/runs/SESSION_ID > /server/path/acceptance.json
```

Exit 0 means complete verified process coverage and paired evaluation, **not**
automatic biological improvement. Exit 2 means an unfinished route or explicit
coverage gaps, including legitimate skips (e.g. no internal gaps for TGS); it
must not be reported as that tool having run. Exit 1 identifies inconsistent or
missing required evidence. The JSON includes each occurrence's method outcomes,
verified execution/collection refs, candidate/output handoffs, final assembly,
and paired quality metrics. A failed/blocked process requires inspection of its
recorded logs; a missing binary on the development host is not code failure.

Keep the run directory, frozen inputs, indexed artifacts, process logs and
request-bound decisions for acceptance. Do not edit a frozen run to retry new
configuration; create a new session. Use the ordinary inspect/review/resume
commands below for interrupted human gates.

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
