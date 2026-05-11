# 03 Flye, QUAST, And Human Gate Smoke

Task status: planned.

## Goal

Run the Phase 4 mainline closure:

```text
FOFN / long reads
-> Flye
-> assembly.fasta
-> QUAST
-> quast_summary.json
-> blocked_human
```

This task proves that `assembly` can produce a real draft assembly and
`assembly_review` can produce the first real review report, while still
requiring a human decision before promotion.

## Scope

Use only the primary Flye route for ONT/CLR draft assembly and the existing
QUAST route for reference-free assembly review.

Flye must require explicit inputs and parameters:

- long-read input or FOFN
- read type
- estimated genome size
- threads

Missing required Flye parameters should produce `blocked_param`, not an
invented default and not an attempted run.

QUAST should run only after a materialized assembly FASTA exists and the QUAST
environment/tool preflight succeeds.

## Boundaries

- Do not run NextDenovo or Canu as automatic fallback assemblers.
- Do not use hifiasm for this ONT/CLR mainline.
- Do not run BUSCO, Merqury, mapping coverage, purge_dups, or contamination
  checks in this task.
- Do not polish, purge, scaffold, repeat-annotate, gene-annotate, or
  function-annotate in this task.
- Do not auto-accept or auto-reject assemblies from QUAST metrics.
- Do not invent QUAST thresholds.
- Do not continue past `blocked_human` after generated-candidate QUAST review.

## Expected Outputs

Assembly:

- Flye command stdout/stderr
- Flye log if produced
- materialized `assembly.fasta` or existing artifact equivalent
- executed Flye candidate bundle
- ledger attempt lifecycle events

Assembly review:

- QUAST command stdout/stderr
- QUAST `report.tsv`
- optional QUAST `report.txt`
- `quast_summary.json`
- updated assembly evaluation bundle
- `assembly_review_request.json`
- final state `blocked_human`

Status vocabulary should distinguish:

- `completed` for successful method-level artifacts before review blocking
- `blocked_param`
- `blocked_input`
- `blocked_env`
- `missing_tool`
- `failed_retryable`
- `failed_fatal`
- `blocked_human`

## Tasks

- [ ] Add Flye parameter validation before local Flye execution.
- [ ] Require explicit `read_type`, `estimated_genome_size`, and `threads` for
  Phase 4 local Flye smoke.
- [ ] Route missing Flye parameter cases to `blocked_param` with a clear missing
  parameter list.
- [ ] Ensure Flye runs through the configured `env_assembly_flye` environment.
- [ ] Register the materialized Flye assembly FASTA and command artifacts.
- [ ] Ensure Flye failure does not trigger NextDenovo, Canu, or hifiasm.
- [ ] Ensure QUAST runs through the configured `env_assembly_review`
  environment.
- [ ] Parse QUAST report into `quast_summary.json` without applying thresholds.
- [ ] Write or update the review request artifact with candidate id, Flye
  provenance, QUAST artifact refs, and non-decisional metrics.
- [ ] Ensure generated assemblies stop at `blocked_human` after QUAST.
- [ ] Add tests for missing Flye params, missing Flye env/tool, Flye success,
  QUAST success, QUAST missing env/tool, and no auto-promotion.
- [ ] Add a documented small-real-data smoke command.

## Acceptance

- A small real long-read smoke can produce a real Flye assembly when the Flye
  environment and required parameters exist.
- QUAST evaluates the materialized assembly when the QUAST environment exists.
- QUAST output is registered and summarized.
- The workflow stops at `blocked_human`.
- No final assembly is promoted without a human review decision.
- Non-mainline review methods remain planning-only or blocked-reason records.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_assembly_review tests.test_review_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Suggested real smoke command after implementation:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent gene-agent run \
  --session-id phase4-flye-quast-smoke \
  --manifest tests/fixtures/phase4/flye_quast_manifest.yaml \
  --data-dir data/phase4_smoke
```

## Human Decisions Before Coding

- Exact source of `estimated_genome_size` for Phase 4: manifest hint,
  `stage_outputs.assembly.flye_parameters`, or a separate structured request.
- Accepted Flye `read_type` values for this phase, such as `nano-raw`,
  `nano-hq`, or `pacbio-raw`.
- Whether missing Flye/QUAST env should block the stage or fail terminally in
  local execution mode.
- Whether `quast_summary.json` should be a new artifact type or stored as
  metadata inside the existing evaluation bundle.

