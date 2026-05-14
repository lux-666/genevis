# 03 Existing Assembly To RepeatMasker Backbone

Task status: planned.

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

- [ ] Confirm existing assembly inventory route skips read QC, genome survey, and
  assembly execution.
- [ ] Define or update a manifest template for existing assembly plus provided
  repeat library.
- [ ] Define or update a run-config template for local RepeatMasker.
- [ ] Verify RepeatMasker env preflight through `env_dispatch`.
- [ ] Run RepeatMasker on server/HPC or with a small fixture/fake binary in tests.
- [ ] Confirm `softmasked_genome_ref` is passed to `gene_annotation`.
- [ ] Confirm `gene_annotation` planning does not require real BRAKER/MAKER/etc.
  execution.
- [ ] Confirm `function_annotation` requires database provenance and does not
  fake a completed annotation table.
- [ ] Add tests for existing assembly entry, RepeatMasker success/failure,
  downstream handoff, and provenance gate.
- [ ] Record server-side smoke results in `docs/handoff.md`.

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
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
