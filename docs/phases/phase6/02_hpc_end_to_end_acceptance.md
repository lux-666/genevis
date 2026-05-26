# 02 HPC End-To-End Acceptance

Task status: not started.

## Goal

Run a single session on HPC that starts from raw long reads and reaches
`current_stage=done`, `supervision_state=completed` — covering every stage in
`STAGE_ORDER` with real tool execution where wired, planning-only where not, and
durable resume across the two mandatory human gates.

Previous HPC validations are per-task fragments: Phase 4 stopped at
`assembly_review`, Phase 5 task 03 started from an existing assembly, Phase 5
task 04 auto-review bypassed the human gate, Phase 5 task 11 function
provenance used provided annotation only. No single session has exercised the
full `raw_long_reads → read_qc → genome_survey → assembly → assembly_review
(human resume) → hic_scaffolding (human resume or skip) → repeat_annotation →
gene_annotation → function_annotation → done` chain with durable checkpoint
resume at every gate.

This task is the Phase 6 baseline: if the chain cannot finish end-to-end, the
observability and hardening work in later Phase 6 tasks has nothing to observe.

## Scope

- Prepare a manifest and run-config for a raw-reads-to-completion run on the
  HPC host under `/data/work/genevis`.
- Execute real tools where the current `local_exec` surface supports them:
  `read_qc` (SeqKit stats), `genome_survey` (Jellyfish + GenomeScope2 if short
  DNA reads are available; skip otherwise), `assembly` (Flye),
  `assembly_review` (QUAST + BUSCO if offline lineage is available),
  `repeat_annotation` (RepeatMasker with provided library).
- Planning-only for: `hic_scaffolding` (no Hi-C reads in this smoke),
  `gene_annotation`, `function_annotation` (with provided proteins and database
  provenance to reach terminal success).
- Exercise the two mandatory human gates with durable resume:
  1. `assembly_review`: `gene-agent inspect` → `--write-decision-template` →
     operator fills decision → `gene-agent resume`.
  2. `hic_scaffolding`: same inspect/resume pattern, or deterministic skip if
     no Hi-C reads are registered.
- Verify `current_stage=done` and `supervision_state=completed` at terminal
  state.
- Verify `final_report/review_decision_report.json` artifact is written.
- Record all session details in `docs/handoff.md`.

## Non-Goals

- Do not add new bioinformatics tool execution beyond the current wired set.
- Do not add automatic biological acceptance thresholds.
- Do not implement pause/retry/rebuild lifecycle commands.
- Do not change graph, schema, or stage code — this task is a validation pass
  over existing implementation, not a feature task.
- Do not exercise the LLM `auto` review policy in this acceptance run; use
  `strict` mode with explicit human decisions.

## Prerequisites

- The HPC host has the `gene_agent` conda environment with all dependencies
  installed (`pip install -e .` from the repo checkout).
- `scripts/server_check.sh` passes.
- The HPC host has SeqKit, Flye, QUAST installed in `env_geno_exec` or the
  stage-specific conda env.
- A RepeatMasker environment is available for `repeat_annotation`.
- Input files:
  - Raw long reads FASTQ/FASTA (the same `bht.Nanopore.pass.first500.fa` used
    in Phase 4 smoke is sufficient, or a larger real dataset if available).
  - A provided repeat library FASTA (can reuse the Phase 5 task 03 fixture).
  - Provided predicted proteins FASTA for function annotation provenance (can
    reuse the Phase 5 task 04/11 fixture).

## Manifest Template

```yaml
session_id: phase6-e2e-YYYYMMDD-a
sample_id: phase6_e2e_smoke
target_assembly_level: contig
assets:
  - asset_type: raw_long_reads
    paths:
      - /data/work/genevis/data/test/bht.Nanopore.pass.first500.fa
    platform: cycloneseq
    layout: single
    format: fasta
    compression: none
    metadata:
      source: cycloneseq_basecaller
  - asset_type: repeat_library
    paths:
      - /data/work/genevis/data/phase5_existing_asm_smoke/provided_repeat_library.fa
    metadata:
      source: operator_provided
      trusted: true
  - asset_type: predicted_proteins
    paths:
      - /data/work/genevis/data/phase5_auto_review/provided_proteins.faa
    metadata:
      source: operator_provided
```

Adjust absolute paths to match the HPC data layout. Do not commit files with
private paths.

## Run-Config Template

```json
{
  "local_exec": [
    "read_qc",
    "assembly",
    "assembly_review",
    "repeat_annotation"
  ],
  "shared_parameters": {
    "read_type": "ont_raw",
    "estimated_genome_size": 5000000
  },
  "assembly": {
    "flye_parameters": {
      "threads": 4
    }
  },
  "assembly_review": {
    "review_policy": {
      "mode": "strict"
    }
  },
  "function_annotation": {
    "database_provenance": [
      {
        "method_name": "function_annotation.candidate.interproscan",
        "database_name": "InterPro",
        "database_version": "99.0",
        "snapshot_date": "2026-05-25",
        "database_source_ref": "/data/db/interpro/99.0",
        "descriptor_version": "interproscan.yaml",
        "input_proteins_ref": "__REPLACE_WITH_PROTEINS_FASTA_REF__",
        "command": "interproscan.sh -i proteins.faa -d interpro_99"
      }
    ]
  }
}
```

## Operator Walkthrough

### Step 1: Initial Run

```bash
cd /data/work/genevis

# Prepare data dir
mkdir -p data/phase6_e2e/out

# Patch proteins ref into run-config
PROTEINS_REF=$(python3 -c "
import yaml; from pathlib import Path
m = yaml.safe_load(Path('data/phase6_e2e/manifest.yaml').read_text())
for a in m.get('assets', []):
    if a['asset_type'] == 'predicted_proteins':
        p = Path(a['paths'][0])
        print(p if p.is_absolute() else (Path('data/phase6_e2e') / p).resolve())
        break
")
sed "s|__REPLACE_WITH_PROTEINS_FASTA_REF__|${PROTEINS_REF}|g" \
  data/phase6_e2e/run_config_template.json > data/phase6_e2e/out/run_config.json

# Run
conda run -n gene_agent gene-agent run \
  --manifest    /data/work/genevis/data/phase6_e2e/manifest.yaml \
  --run-config  /data/work/genevis/data/phase6_e2e/out/run_config.json \
  --session-id  phase6-e2e-YYYYMMDD-a \
  --data-dir    /data/work/genevis/data/phase6_e2e/out
```

Expected: blocks at `assembly_review` with `supervision_state=blocked_human`.

### Step 2: Assembly Review Gate

```bash
# Inspect
conda run -n gene_agent gene-agent inspect \
  --session-id phase6-e2e-YYYYMMDD-a \
  --data-dir   /data/work/genevis/data/phase6_e2e/out

# Generate decision template
conda run -n gene_agent gene-agent inspect \
  --session-id phase6-e2e-YYYYMMDD-a \
  --data-dir   /data/work/genevis/data/phase6_e2e/out \
  --write-decision-template /data/work/genevis/data/phase6_e2e/out/assembly_review_decision.json

# Edit the decision file: set decision=accept, fill candidate_id and
# final_assembly_ref from the template guidance, add operator notes.

# Resume
conda run -n gene_agent gene-agent resume \
  --session-id   phase6-e2e-YYYYMMDD-a \
  --decision-file /data/work/genevis/data/phase6_e2e/out/assembly_review_decision.json \
  --data-dir     /data/work/genevis/data/phase6_e2e/out
```

Expected: workflow advances past `assembly_review`. If no Hi-C reads are
registered, `hic_scaffolding` should skip automatically. Otherwise it blocks at
`hic_scaffolding` for a second resume.

### Step 3: Verify Terminal State

If the workflow does not block at `hic_scaffolding` (no Hi-C reads), it should
continue through `repeat_annotation` → `gene_annotation` → `function_annotation`
→ `done`.

```bash
# Final inspect
conda run -n gene_agent gene-agent inspect \
  --session-id phase6-e2e-YYYYMMDD-a \
  --data-dir   /data/work/genevis/data/phase6_e2e/out \
  --json
```

Expected output fields:
- `current_stage: done`
- `supervision_state: completed`

### Step 4: Verify Artifacts

```bash
# Final report
cat /data/work/genevis/data/phase6_e2e/out/artifacts/final_report/phase6-e2e-YYYYMMDD-a/review_decision_report.json

# Ledger events
wc -l /data/work/genevis/data/phase6_e2e/out/ledger/phase6-e2e-YYYYMMDD-a/events.jsonl

# Key artifact dirs should exist
ls /data/work/genevis/data/phase6_e2e/out/artifacts/read_qc/
ls /data/work/genevis/data/phase6_e2e/out/artifacts/assembly/
ls /data/work/genevis/data/phase6_e2e/out/artifacts/assembly_review/
ls /data/work/genevis/data/phase6_e2e/out/artifacts/repeat_annotation/
ls /data/work/genevis/data/phase6_e2e/out/artifacts/function_annotation/
```

## Acceptance Criteria

1. A single `session_id` reaches `current_stage=done`,
   `supervision_state=completed` from raw long reads.
2. The run exercises durable checkpoint resume at `assembly_review` (and
   `hic_scaffolding` if Hi-C reads are present).
3. `final_report/review_decision_report.json` exists and contains at least one
   decision record.
4. The ledger (`events.jsonl`) contains:
   - `session_created`
   - `attempt_started` / `attempt_succeeded` for real-executed methods
   - `review_requested` for `assembly_review`
   - `review_decision_committed` or `auto_decision_committed`
   - `artifact_registered` for final report, function bundle, and repeat
     annotation bundle
5. No duplicate `attempt_started` events for the same method after resume
   (the Phase 6 exit criterion for idempotent resume).
6. All observations recorded in `docs/handoff.md` Phase 6 section.

## Verification

After completion, the following should be verifiable from the HPC data dir:

```bash
# Terminal state
conda run -n gene_agent gene-agent inspect \
  --session-id <SESSION> --data-dir <DATA_DIR> --json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['current_stage']=='done'; assert d['supervision_state']=='completed'"

# No duplicate attempts
python3 -c "
import json; from pathlib import Path
events = [json.loads(l) for l in Path('<DATA_DIR>/ledger/<SESSION>/events.jsonl').read_text().splitlines()]
starts = [(e['method_name'], e['attempt_id']) for e in events if e['event_type']=='attempt_started']
assert len(starts) == len(set(starts)), f'duplicate attempt_started: {starts}'
"

# Final report exists
test -f <DATA_DIR>/artifacts/final_report/<SESSION>/review_decision_report.json
```

## Recording

After the run, update `docs/handoff.md` with a "Phase 6 End-To-End Acceptance
Run Record" section containing:

- host, session id, manifest path, run-config path, data dir
- route trace
- final `current_stage` and `supervision_state`
- human gate decisions made (stage, decision, decided_by)
- key artifact paths (final report, function bundle, repeat annotation bundle,
  assembly draft)
- ledger event count
- any bugs found and fixed during the run
