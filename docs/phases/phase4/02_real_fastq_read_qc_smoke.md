# 02 Real FASTQ Read QC Smoke

Task status: implemented (env resolver wiring, structured blocked states, committed fixture, and runnable real-data smoke through the CLI).

## Goal

Run the first small-real-data chain:

```text
real FASTQ
-> inventory
-> seqkit stats
-> read_qc_summary.json
-> raw reads FOFN
```

This task proves that `gene_agent` can register a real input file, execute a
real installed tool, write inspectable artifacts, and update ledger records.

## Scope

Use SeqKit through the environment resolution from task 01.

The smoke should use a small FASTQ fixture or a clearly documented local fixture
path. The data must be small enough for routine local testing and must not
require Flye or QUAST.

Expected method:

```text
read_qc.long_read_stats.seqkit_stats
```

Optional method:

```text
read_qc.length_filter.seqkit_seq
```

Length filtering should remain optional and require explicit approved
thresholds. The default smoke should not invent filter thresholds.

## Boundaries

- Do not use fake SeqKit output for the real smoke acceptance.
- Do not invent length or quality thresholds.
- Do not start assembly in this task.
- Do not require Flye, QUAST, Jellyfish, GenomeScope2, BUSCO, or annotation
  databases.
- Do not store FASTQ contents in graph state.
- Do not mark read QC completed if SeqKit did not run successfully in local
  execution mode.

## Expected Outputs

- `seqkit_stats.tsv`
- `read_qc_summary.json`
- `raw_reads.fofn` or the existing stable equivalent `long_reads.fofn`
- command stdout and stderr artifacts
- ledger attempt lifecycle events
- compact read QC state with artifact refs and summary fields

Status vocabulary should distinguish:

- `completed`
- `blocked_input`
- `blocked_env`
- `missing_tool`
- `failed_retryable`
- `failed_fatal`

## Tasks

- [x] Add or document a small real FASTQ fixture for Phase 4 read QC smoke.
- [x] Add a manifest fixture that registers the FASTQ as `raw_long_reads`.
- [x] Ensure `seqkit stats -a -T` runs through the configured read QC conda env.
- [x] Write `seqkit_stats.tsv` as a registered artifact.
- [x] Write `read_qc_summary.json` as a compact parsed summary artifact.
- [x] Write a stable FOFN handoff artifact for downstream assembly.
- [x] Ensure ledger records attempt start, success/failure, and artifact
  registration.
- [x] Add tests for missing FASTQ path -> `blocked_input` or clear terminal
  input failure.
- [x] Add tests for missing SeqKit tool -> `missing_tool` / `blocked_env`.
- [x] Add a documented smoke command that a human can run locally.

## Acceptance

- Given a small real FASTQ and the configured `env_geno_exec` environment
  containing SeqKit, `gene_agent` runs SeqKit for real and writes SeqKit stats.
- The FOFN points to the real read input or approved filtered read artifact.
- The output files are inspectable on disk and registered in artifact records.
- The ledger records the real attempt and command result.
- Downstream stages may still remain planning or blocked; this task only
  commits read QC and the FOFN handoff.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_inventory -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Suggested real smoke command after implementation:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent gene-agent run \
  --session-id phase4-readqc-smoke \
  --manifest tests/fixtures/phase4/read_qc_manifest.yaml \
  --data-dir data/phase4_smoke \
  --local-exec read_qc
```

Without `--local-exec read_qc` the read QC stage stays in planning-only mode
and only a FOFN is committed; `--local-exec read_qc` is what opts the stage
into real SeqKit execution through the configured conda env.

## Human Decisions Before Coding

- Whether the committed FOFN artifact should be named `raw_reads.fofn` or keep
  the existing `long_reads.fofn` name.
- Whether the small FASTQ fixture can be committed to the repo or should remain
  a documented local fixture.

## Implemented State

- Committed `tests/fixtures/phase4/smoke.fastq` (three small reads) and
  `tests/fixtures/phase4/read_qc_manifest.yaml` registering it as
  `raw_long_reads` for both tests and documented smoke runs. The real-biology
  CycloneSEQ FASTQ smoke remains a human-supplied local file.
- Existing FOFN names `long_reads.fofn` / `filtered_long_reads.fofn` are kept
  as the stable downstream handoff so the assembly stage contract is unchanged.
- Inventory now absolutises relative manifest paths against the manifest's
  own directory (falling back to cwd). Absolute paths stay as the operator
  spelled them (no `/private/var` canonicalisation surprise on macOS). The
  asset registry, frozen attempt scripts that `cd` into `data/work/...`, and
  the committed FOFN all end up referencing the same reachable absolute path.
- `read_qc` resolves the configured conda env via the shared Phase 4 task01
  dispatcher (`app/execution/env_dispatch.py:resolve_method_env` +
  `attach_conda_fields`) before freezing each attempt. The single shared
  execution env is `env_geno_exec`; `gene_agent` stays the orchestrator env.
  When the caller supplies an explicit `stage_outputs.read_qc.env.PATH` the
  dispatcher steps aside so existing fake-binary tests continue to run.
- Preflight outcomes map to structured read_qc statuses:
  `blocked_env` when conda or the declared env is not usable,
  `missing_tool` when a declared required tool is absent in the env.
  Both route to the stage's terminal failure edge, set `supervision_state`
  to `failed_terminal`, and record an `env_preflight_blocked` ledger event
  with `env_target`, `missing_tools`, and detail.
- A missing raw_long_reads file path now surfaces as `status: "blocked_input"`
  instead of a generic `failed`, with the unreachable path kept in
  `blocked_input_ref`.
- `collect_seqkit_artifacts` now writes a compact `read_qc_summary.json`
  alongside `seqkit_stats.tsv`, registers it as artifact type
  `read_qc.long_read_summary`, and exposes the ref as
  `stage_outputs.read_qc.summary_artifact_ref`. The parsed summary (num_seqs,
  sum_len, n50, q20/q30/avg_qual, etc.) remains available as state
  `stage_outputs.read_qc.summary` for downstream stages.
- Length filtering follows the same env-dispatcher wiring and blocked-state
  mapping as the stats method.
- CLI `gene-agent run` gained a repeatable `--local-exec STAGE` flag that
  seeds `stage_outputs[STAGE].execution_mode = "local"` before the graph
  starts. Without this flag `read_qc` stays in the planning-only path and
  only the FOFN is committed; supplying `--local-exec read_qc` is what opts
  the stage into real SeqKit execution.

## Suggested Local Smoke

With `seqkit` installed inside the configured `env_geno_exec` conda env:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent gene-agent run \
  --session-id phase4-readqc-smoke \
  --manifest tests/fixtures/phase4/read_qc_manifest.yaml \
  --local-exec read_qc
```

Expected outcome:

- `data/artifacts/read_qc/phase4-readqc-smoke/seqkit_stats.tsv`
- `data/artifacts/read_qc/phase4-readqc-smoke/read_qc_summary.json`
- `data/artifacts/read_qc/phase4-readqc-smoke/long_reads.fofn`
  (absolute path to the fixture FASTQ)
- ledger events for attempt_started / attempt_succeeded / artifact_registered
- the graph continues past read_qc into the assembly mainline until it
  reaches the human gate at `assembly_review`

If conda or `seqkit` is not resolvable inside `env_geno_exec`, the workflow
terminates at `read_qc` with `status` equal to `blocked_env` or
`missing_tool` and a matching `env_preflight_blocked` ledger event. A real
`tests.test_read_qc.Phase4ReadQCRealSmokeTests` test exercises this full
path through the CLI when `env_geno_exec` is available; it is skipped
automatically otherwise.
