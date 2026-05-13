# 04 Real Biology Mainline Acceptance

Task status: planned.

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

Until task 05 adds a structured parameter entry point, use the Python one-liner
shape from `03_flye_quast_human_gate.md` and point it at a local manifest:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python - <<'PY'
from app.graph.builder import build_graph
from app.schemas.state import initial_state

state = initial_state(
    session_id="phase4-real-mainline-smoke",
    input_manifest_ref="<local-manifest-with-raw_long_reads>.yaml",
    data_dir="data/phase4_real_smoke",
)
state["stage_outputs"] = {
    "read_qc": {"execution_mode": "local"},
    "assembly": {
        "execution_mode": "local",
        "flye_parameters": {
            "read_type": "nano-raw",
            "estimated_genome_size": 2000000,
            "threads": 4,
        },
    },
    "assembly_review": {"execution_mode": "local"},
}

graph = build_graph()
result = graph.invoke(state, config={"configurable": {"thread_id": state["session_id"]}})
print("current_stage:", result.get("current_stage"))
print("supervision_state:", result.get("supervision_state"))
print("active_interrupt:", result.get("active_interrupt"))
PY
```

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
