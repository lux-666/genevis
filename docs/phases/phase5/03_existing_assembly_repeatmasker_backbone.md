# 03 Existing Assembly To RepeatMasker Backbone

Task status: implemented in Mac-side code/tests; server/HPC validation pending.

## Goal

Run the first downstream annotation backbone on a real or fixture existing
assembly without expanding the whole annotation tool catalog.

Target chain:

```text
existing assembly manifest
-> inventory
-> assembly_review deterministic pass-through or human accept
-> hic_scaffolding skip if no Hi-C
-> repeat_annotation local RepeatMasker with provided library
-> gene_annotation planning from softmasked genome
-> function_annotation database provenance gate
```

This task proves that users can enter the workflow with an existing assembly and
reach the repeat/gene/function backbone without rerunning read QC, genome survey,
or assembly.

## Scope

- Use `assembly_fasta_registered` / existing assembly input as the smoke start.
- Use a provided repeat library for RepeatMasker.
- Run only the existing explicit local RepeatMasker route.
- Register real RepeatMasker outputs:
  - softmasked genome
  - repeat annotation GFF / table as supported by the current collector
  - command logs
- Pass `softmasked_genome_ref` to `gene_annotation`.
- Let `gene_annotation` produce planning artifacts from the softmasked genome.
- Let `function_annotation` stop clearly when database provenance is missing or
  incomplete.
- Add server/HPC smoke templates for existing assembly + provided repeat
  library.

## Non-Goals

- Do not run RepeatModeler, EDTA, or TRF de novo repeat-library discovery.
- Do not run BRAKER3, MAKER, AUGUSTUS, Miniprot, HISAT2, StringTie, IsoQuant,
  GeMoMa, EVM, or ANNEVO.
- Do not run DIAMOND, InterProScan, eggNOG-mapper, or KofamScan.
- Do not download annotation databases.
- Do not invent gene model merge policy.
- Do not rerun Flye for existing assembly inputs.
- Do not auto-accept generated assemblies from metrics.

## Expected Outputs

- Manifest template for existing assembly + provided repeat library.
- Run-config template enabling local RepeatMasker only.
- Artifact records for:
  - existing assembly registry
  - RepeatMasker command logs
  - softmasked genome
  - repeat annotation output
  - gene annotation planning artifact
  - function annotation blocked/provenance artifact, if applicable
- Ledger events for RepeatMasker attempt lifecycle and downstream stage
  commits.
- Final state demonstrates honest downstream routing rather than fake terminal
  completion.

## Tasks

- [x] Confirm existing assembly inventory route skips read QC, genome survey, and
  assembly execution.
- [x] Define or update a manifest template for existing assembly plus provided
  repeat library.
- [x] Define or update a run-config template for local RepeatMasker.
- [x] Verify RepeatMasker env preflight through stage / env-resolver tests;
  real server preflight remains part of the pending HPC smoke.
- [x] Run RepeatMasker on server/HPC or with a small fixture/fake binary in tests.
- [x] Confirm `softmasked_genome_ref` is passed to `gene_annotation`.
- [x] Confirm `gene_annotation` planning does not require real BRAKER/MAKER/etc.
  execution.
- [x] Confirm `function_annotation` requires database provenance and does not
  fake a completed annotation table.
- [x] Add tests for existing assembly entry, RepeatMasker success/failure,
  downstream handoff, and provenance gate. Failure is covered by both
  stage-level tests and a CLI fake-binary failure E2E.
- [ ] Record server-side smoke results in `docs/handoff.md`.

## Implementation Notes

- Example inputs are in
  `docs/examples/phase5_existing_assembly_repeatmasker_manifest.template.yaml`
  and `docs/examples/phase5_repeatmasker_run_config.template.json`.
- `scripts/server_phase5_repeatmasker_smoke.sh` is the server-side entry point.
  It expects the workflow to reach `function_annotation` and exit non-zero
  because database provenance is missing. The script also validates the JSON
  contract: RepeatMasker bundle status/source, gene bundle softmasked-genome
  handoff, artifact index types, and absence of a function bundle.
- `tests.test_entrypoints.Phase5RepeatMaskerEntrypointE2ETests` runs the
  existing-assembly backbone through the real parent graph on Mac. The success
  case bypasses env preflight so the fake `RepeatMasker` binary can run; a
  separate preflight-block case does not patch `resolve_method_env` and proves
  the CLI path stops at the RepeatMasker env gate before command execution. It
  verifies:
  - existing assembly routing skips read QC / genome survey / assembly through
    the normal inventory and parent-graph path
  - local RepeatMasker writes softmasked genome and GFF3 artifacts
  - RepeatMasker preflight blocks before work directories and downstream gene
    artifacts are written when `RepeatMasker` is missing from the resolved env
  - RepeatMasker command failure stops at `repeat_annotation` and does not write
    a gene annotation bundle
  - `gene_annotation` consumes the softmasked genome and writes only planning
    artifacts
  - `function_annotation` stops at the missing `database_provenance` gate

## Pending HPC Validation

Run after `git pull` on the server/HPC checkout:

```bash
bash scripts/server_check.sh
bash scripts/server_test.sh
bash scripts/server_phase4_smoke.sh \
  --manifest    data/phase4_real_smoke/manifest.yaml \
  --run-config  data/phase4_real_smoke/run_config.json \
  --session-id  phase4-server-smoke-YYYYMMDD-a \
  --data-dir    data/phase4_real_smoke/out
bash scripts/server_phase5_repeatmasker_smoke.sh \
  --manifest    data/phase5_existing_asm_smoke/manifest.yaml \
  --run-config  data/phase5_existing_asm_smoke/run_config.json \
  --session-id  phase5-existing-asm-YYYYMMDD-a \
  --data-dir    data/phase5_existing_asm_smoke/out
```

Record real artifact refs and command outcomes in `docs/handoff.md` before
marking server-side acceptance complete.

## Acceptance

- Existing assembly inputs can enter the downstream backbone without rerunning
  assembly.
- RepeatMasker runs locally through the configured execution environment when a
  provided repeat library exists.
- The softmasked genome is a real artifact and is consumed by gene annotation.
- Function annotation does not complete without explicit database provenance.
- The full route remains inspectable through artifact refs and ledger events.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_inventory tests.test_repeat_annotation tests.test_gene_annotation tests.test_function_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
