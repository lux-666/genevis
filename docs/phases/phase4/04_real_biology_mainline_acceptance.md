# 04 Real Biology Mainline Acceptance

Task status: partially implemented. First real-biology acceptance pass was
run on `data/test/bht.Nanopore.pass.first500.fa` (500 Nanopore reads
packaged as FASTA, 1.49 Mbp total) against `env_geno_exec` on
darwin-arm64. `read_qc → assembly.candidate.flye` executed with real
binaries and stopped at the `assembly_review` human gate as
`supervision_state = "blocked_human"`. QUAST was skipped because this
host has no `quast.py` for arm64. Full run record, artifact refs, and
follow-up findings live under the "Phase 4 Real Biology Acceptance Run
Record" section in `docs/handoff.md`. A second acceptance pass on a
host with `quast.py` is still pending to close the QUAST half of the
gate.

## Goal

Run the Phase 4 mainline on a human-supplied real CycloneSEQ / PacBio FASTQ
and confirm that the workflow produces real biological artifacts before Phase 5
execution is expanded.

Target chain:

```text
inventory
-> read_qc local SeqKit
-> stable long_reads.fofn
-> assembly local Flye
-> assembly_review local QUAST
-> blocked_human
```

This task is an acceptance pass over already implemented Phase 4 code. It should
surface environment, path, parameter, artifact, and ledger issues without adding
new method families.

## Scope

- Use a real local FASTQ supplied by the human operator.
- Use the configured `env_geno_exec` conda environment.
- Run only the Phase 4 mainline methods:
  - `read_qc.long_read_stats.seqkit_stats`
  - `assembly.candidate.flye`
  - `assembly_review.eval.quast`
- Require explicit Flye parameters:
  - `read_type`
  - `estimated_genome_size`
  - `threads`
- Confirm that generated assemblies stop at the human review gate.

## Boundaries

- Do not commit large real FASTQ files to the repository.
- Do not invent genome size, read type, thread count, or biological thresholds.
- Do not run BUSCO, Merqury, purge_dups, contamination checks, NextDenovo, Canu,
  hifiasm, Hi-C, repeat annotation, gene annotation, or function annotation.
- Do not auto-accept or auto-reject an assembly from QUAST metrics.
- Do not implement RemoteRunner, requested-stage routing, durable resume, or
  database downloads in this task.

## Expected Outputs

- `data/artifacts/read_qc/<session>/seqkit_stats.tsv`
- `data/artifacts/read_qc/<session>/read_qc_summary.json`
- `data/artifacts/read_qc/<session>/long_reads.fofn`
- `data/artifacts/assembly/<session>/flye/draft_assembly.fasta`
- `data/artifacts/assembly_review/<session>/quast/.../report.tsv`
- `data/artifacts/assembly_review/<session>/quast/.../quast_summary.json`
- `data/artifacts/assembly_review/<session>/assembly_review_request.json`
- ledger events for session creation, attempts, artifact registration, and
  review request
- final state:
  - `current_stage: assembly_review`
  - `supervision_state: blocked_human`

## Tasks

- [ ] Prepare a local manifest that registers the real FASTQ as
  `raw_long_reads`.
- [ ] Confirm `env_geno_exec` preflight resolves `seqkit`, `flye`, and
  `quast.py`.
- [ ] Run the mainline with local execution enabled for `read_qc`, `assembly`,
  and `assembly_review`.
- [ ] Provide explicit Flye parameters from the human operator.
- [ ] Verify read QC artifacts and FOFN paths are reachable from attempt working
  directories.
- [ ] Verify Flye produces a materialized `draft_assembly.fasta`.
- [ ] Verify QUAST produces `report.tsv` and `quast_summary.json`.
- [ ] Verify `assembly_review_request.json` references the Flye candidate and
  QUAST summary artifact.
- [ ] Verify the final state is `blocked_human` and no final assembly is
  promoted.
- [ ] Record the smoke command, session ID, artifact refs, and observed issues in
  `docs/handoff.md`.

## Acceptance

- A real local FASTQ run produces all expected Phase 4 artifacts.
- The artifact registry and ledger contain enough provenance to inspect what ran.
- The workflow pauses for human review after QUAST.
- Any failure is represented as `blocked_input`, `blocked_param`,
  `blocked_env`, `missing_tool`, `failed_retryable`, or `failed_fatal` rather
  than a fake success.
- Phase 5 execution is not started until this task is complete.

## Suggested Manual Run Shape

Phase 4 task 05 added `gene-agent run --run-config <file>.json`, so the
real-biology smoke no longer needs a Python one-liner. Write a small
`run_config.json`:

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review"],
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 2000000,
      "threads": 4
    }
  }
}
```

Then drive the mainline with:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  gene-agent run \
    --session-id phase4-real-mainline-smoke \
    --manifest <local-manifest-with-raw_long_reads>.yaml \
    --data-dir data/phase4_real_smoke \
    --run-config run_config.json
```

Expected end state when `env_geno_exec` has `seqkit`, `flye`, and
`quast.py`:

- `supervision_state: blocked_human`
- `current_stage: assembly_review`
- `data/artifacts/read_qc/<session>/{seqkit_stats.tsv, read_qc_summary.json, long_reads.fofn}`
- `data/artifacts/assembly/<session>/flye/draft_assembly.fasta`
- `data/artifacts/assembly_review/<session>/quast/<candidate>/{report.tsv, quast_summary.json}`
- `data/artifacts/assembly_review/<session>/assembly_review_request.json`

The Python one-liner shape in
`03_flye_quast_human_gate.md` is kept only as a reference for
environments where `--run-config` is unavailable.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
