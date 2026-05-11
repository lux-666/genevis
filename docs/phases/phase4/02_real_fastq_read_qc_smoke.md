# 02 Real FASTQ Read QC Smoke

Task status: planned.

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

- [ ] Add or document a small real FASTQ fixture for Phase 4 read QC smoke.
- [ ] Add a manifest fixture that registers the FASTQ as `raw_long_reads`.
- [ ] Ensure `seqkit stats -a -T` runs through the configured read QC conda env.
- [ ] Write `seqkit_stats.tsv` as a registered artifact.
- [ ] Write `read_qc_summary.json` as a compact parsed summary artifact.
- [ ] Write a stable FOFN handoff artifact for downstream assembly.
- [ ] Ensure ledger records attempt start, success/failure, and artifact
  registration.
- [ ] Add tests for missing FASTQ path -> `blocked_input` or clear terminal
  input failure.
- [ ] Add tests for missing SeqKit tool -> `missing_tool` / `blocked_env`.
- [ ] Add a documented smoke command that a human can run locally.

## Acceptance

- Given a small real FASTQ and the `gene_agent` environment containing SeqKit,
  `gene_agent` runs SeqKit for real and writes SeqKit stats.
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
  --data-dir data/phase4_smoke
```

## Human Decisions Before Coding

- Whether the committed FOFN artifact should be named `raw_reads.fofn` or keep
  the existing `long_reads.fofn` name.
- Whether the small FASTQ fixture can be committed to the repo or should remain
  a documented local fixture.

