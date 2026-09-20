# Run and Recovery

This is the single operator guide for starting, inspecting, reviewing, and
resuming a Gene Agent session. Behavioral explanations live in
[Runtime orchestration](../architecture/topics/runtime-orchestration.md) and
[Review and resume](../architecture/topics/review-and-resume.md); exact lifecycle
semantics are owned by the
[Run data contract](../architecture/reference/data-contract.md) and its linked
reference contracts. CLI flags are defined by `app/entrypoints/run.py`;
deployment pages link here instead of copying the run contract.

## Acceptance order: code tests before real end-to-end runs

**先单测 / 集成测试 → 小数据真实工具 smoke → 分段 E2E → 最后完整真实 E2E。**

The complete ten-stage template is the final acceptance target, not the next
debugging run. First inventory existing tests, fill demonstrated gaps, then
exercise the small five-stage contig route. A passing fake executable proves
the application contract, not that the real biological tool works. A host
readiness check, a FASTA, or a completed session alone proves neither.

### 1. Audit and complete unit / integration coverage

Use fixtures, fake executables and controlled failures; do not run real large
tools in this layer. Check each relevant stage for invalid/missing inputs,
artifact identity and handoff, router/gate/resume behavior, durable failure
records, and explicit skip conditions. Verify that failed collection cannot
publish a successful handoff, and that resume does not repeat a completed
process. Extend existing tests rather than creating another parallel harness.

The repository already contains the following coverage; this is an inventory
of test code, not a claim that every boundary or real-tool route is accepted:

| Boundary | Existing evidence | Remaining acceptance work |
|---|---|---|
| Stage input validation | `tests/test_inventory.py`, `test_read_qc.py`, `test_genome_survey.py`, `test_assembly.py`, `test_hic_scaffolding.py`, `test_gapcloser.py`, `test_base_polish.py`, `test_t2t.py` | Check the chosen route's actual invalid-input matrix; add uncovered cases before real execution. |
| Artifact handoff | `test_assembly_candidate_bundle.py`, `test_resource_resolution.py`, `test_occurrence_settlement.py`, `test_workflow_plan.py` | Prove continuous handoff with real artifacts in the small E2E, not only isolated fixtures. |
| Router / gate / resume | `test_graph_routing.py`, `test_review_gates.py`, `test_review_application.py`, `test_resume_transaction.py`, `test_durable_resume.py` | Exercise actual tool-backed gates across process/SQLite close and reopen. |
| Failure and skip | `test_execution_lifecycle.py`, `test_execution_collect.py`, `test_ledger_v2.py`, stage suites | Test nonzero exits, missing/malformed products, incomplete execution and permitted skips separately; no false success or automatic rerun. |
| AutoHiC recovery | `test_hic_scaffolding.py::HiCScaffoldingStageTests` contains executed-checkpoint collection replay and adoption/source-retention tests using a fake AutoHiC environment | Real-tool, detached human-gate resume remains a separate slice; assert unchanged execution count. |
| Base Polish recovery | `test_base_polish.py` includes collection replay, corrupt-evidence rejection, paired final evaluation and SQLite/CLI resume using fake tools | Real Racon/minimap2 smoke and representative quality acceptance are still required. |
| Placeholder boundary | `test_registry_executable_invariant.py`, `test_finishing_plan.py`; Base Polish is now executable, not a placeholder | Any future requested placeholder must fail deterministically, record failure, produce no success delivery and enter no downstream stage. Unsupported input is not a placeholder or a successful skip. |

**Test isolation is not yet complete.** `Phase4ReadQCRealSmokeTests` in
`test_read_qc.py` probes `env_geno_exec` during collection and runs real SeqKit
when available. `test_phase10_parameter_catalog.py` can invoke installed hifiasm
for help/version validation; that is not an assembly smoke. TGS-GapCloser's
real smoke is explicitly opt-in. Because `server_test.sh` currently runs the
whole pytest suite, do not label it a guaranteed tool-free test command on a
provisioned server. Before using it as a strict first-layer gate, separate real
tool tests behind explicit opt-in selection, including their collection-time
probes. Missing development-host binaries are not code defects; skipped tests
are not evidence of successful real execution.

### 2. Run small real-tool smoke tests for each key stage

Use bounded, tool-valid input and actually invoke SeqKit, Jellyfish/GenomeScope2,
Flye or the selected compatible assembler, purge_dups, QUAST, BUSCO, AutoHiC,
TGS-GapCloser, and Racon/minimap2 as applicable. Check frozen command/input
identity, exit status, required native products, collection, indexed artifacts,
Ledger outcomes, and failure/recovery behavior. A version/help probe is not
enough. Record tool versions, data provenance/checksums and resource limits.
Do not require a biologically excellent assembly from smoke data.

Existing real-tool entries are the conditional SeqKit test above and the
[opt-in TGS-GapCloser smoke](#hifi-gap-closing-from-an-existing-assembly).
Dedicated small-data real-tool smoke coverage for the other listed methods is
not yet established by the current test suites; their fake-tool tests must not
be counted as substitutes. Use enough data for a tool's actual input constraints
(especially BUSCO and AutoHiC), rather than making tiny invalid input pass by
weakening validation. Test a legitimate skip separately from an execution path.

### 3. First real E2E: the small five-stage contig route

After the code gaps and relevant tool smokes are green, use small but real,
traceable data for **QC → Survey → Assembly → Dedup → Review**, with the
[contig templates below](#five-stage-contig-input). Keep biological facts and
read platforms accurate; choose a dataset suitable for survey and dedup rather
than forcing those stages to run on unsuitable material. Keep human gates
manual during acceptance (`review_policy.max_blocked_seconds: -1`).

Verify each downstream frozen attempt consumes the preceding stage's intended
artifact, not a manually substituted path. Require actual execution evidence
for the intended methods, collected metrics, settled occurrences, request-bound
decisions, and a hash-verified final assembly after detached inspection.
If Dedup or another method legitimately skips, report that coverage gap and
test its execution branch separately. For failures, inspect logs and prove
supported recovery or an explicit new session; do not rewrite frozen evidence
to manufacture completion. See the existing
[five-stage acceptance gap](../architecture/reference/conformance.md#five-stage-real-tool-contig-acceptance-is-not-automated).

### 4. Validate Hi-C as an independent E2E slice

Start from a known-good assembly and a small, sufficient real paired Hi-C
dataset. Exercise AutoHiC → required product collection → human adoption gate
→ accept or decline → detached resume. Use the gate's actual decisions:
`accept_candidate`, `keep_prior_assembly`, or `stop_human_review`; do not invent
a generic `reject` value. `reject_parameters` belongs to parameter approval;
`request_parameter_retry` explicitly requests a fresh attempt at adoption review.

Check every required AutoHiC product and adopted/reused assembly handoff.
Close the process/checkpoint connection at the human gate, resume with a bound
decision, and assert that the completed AutoHiC attempt is not launched again:
no second process/start event, unchanged attempt identity and output hashes.
An explicitly requested fresh parameter retry is different from replay.

### 5. Validate finishing stages independently

Give GapCloser, Base Polish and T2T an existing assembly plus each stage's
required supporting reads/facts; do not rerun QC through Hi-C merely to test
them. For a separate run, declare the verified source FASTA through
`assembly_fasta_registered` so Inventory creates the new run's canonical
candidate/artifact refs. Retain the source run's identity/checksum in the
acceptance evidence; never copy foreign run-local refs into a new checkpoint.

- GapCloser: test a real internal-gap execution and the no-gap reuse branch.
- Base Polish: test real correction, mandatory before/after evidence, adoption
  and source retention, then paired final evaluation. Same-read agreement is
  not independent QV and improvement is not assumed.
- T2T: test supported extension evidence and a no-extension path, plus manual
  adoption/retention and resume without replaying a completed alignment.

The GapCloser templates are already available below. Hi-C/Base Polish/T2T
slice fixtures and real-tool acceptance entry points must be completed and
verified as needed; the full-route template is not a substitute for them.

### 6. Only then run the complete real E2E

Use full-scale or representative real data only after the preceding layers
pass. The [complete route below](#complete-hifihic-server-acceptance) is final
integration/biological acceptance, not the primary way to discover ordinary
input, routing, collection or resume bugs. Its read-only `route_acceptance`
command requires the complete route and is **not** a five-stage or slice
validator. Do not add unused stages simply to satisfy that checker.

**Immediate priority:** fill the demonstrated code-test/isolation gaps, complete
the contig methods' small real-tool smokes, then run the small five-stage E2E.
Do not start the ten-stage run first. This guide sets the acceptance sequence;
it does not claim the pending tests or server runs have already been executed.

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
- `server_test.sh` is the earlier regression gate run immediately after installing
  the orchestrator; New Host Setup owns that command and its position. Its
  current real-tool isolation caveat is documented above.
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

This is the **last** acceptance layer. Complete the code tests, small real-tool
smokes, small contig E2E and independent slices above before starting this run.
The existence of these templates does not establish that those prerequisites
have passed.

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
