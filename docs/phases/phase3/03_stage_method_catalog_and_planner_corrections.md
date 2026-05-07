# 03 Stage Method Catalog And Planner Corrections

Task status: planned.

## Goal

Audit and correct the stage method catalog and planner gates before adding more
real method execution slices.

This task exists because Phase 2 intentionally registered broad planning
descriptors, but several method lists are now too flat for an ONT/CLR mainline.
Before implementing tools stage by stage, split method roles into defaults,
conditional candidates, helpers, and non-default compatibility routes.

## Project Context

Current Phase 2 code and descriptors provide a full backbone:

```text
read_qc -> genome_survey -> assembly -> assembly_review
-> hic_scaffolding -> repeat_annotation -> gene_annotation
-> function_annotation
```

However, `descriptors/registry.yaml` and some Python planner constants still
look like simple method lists. That is not enough for real execution because it
can imply that all tools in a stage should run or that ONT/CLR projects should
default to methods meant for other read types.

The implementation must preserve the existing architecture:

- descriptors define package/method capability facts;
- stage planners choose one route or write a review/skip record;
- graph state stores compact refs only;
- real execution still goes through frozen attempts, runners, artifacts, and
  ledger events.

## Current Audit Findings

### Read QC

Current registry entries:

- `read_qc.long_read_stats.seqkit_stats`
- `read_qc.length_filter.chopper`
- `read_qc.length_filter.fastplong`
- `read_qc.length_filter.filtlong`
- `read_qc.length_filter.seqkit_seq`

Current code already avoids running every filter in a chain. Filtering runs only
when a human-approved request selects the currently executable
`read_qc.length_filter.seqkit_seq` path. FOFN writing also already exists in
code as `read_qc.make_fofn.local`, but it is not represented in the descriptor
catalog.

Corrections:

- Treat `seqkit_stats` as the default stats method.
- Treat `make_fofn` as a default internal preparation method.
- Treat `filtlong`, `chopper`, `fastplong`, and `seqkit_seq` as alternative
  length-filter candidates; choose at most one filter route unless a human
  explicitly approves chaining.
- Add optional long-read QC report planning for NanoPlot:
  `read_qc.long_read_qc.nanoplot`.
- Add an internal package/method representation for FOFN generation if the
  descriptor spec allows internal methods:
  `read_qc.prepare.make_fofn`.

Output assets should remain:

- `clean_long_reads` only when filtering actually runs;
- `long_read_qc_report` when NanoPlot or another report method runs;
- `long_reads.fofn` or `filtered_long_reads.fofn`.

### Genome Survey

Current descriptors are biologically valid but must stay gated:

- `genome_survey.kmer_count.jellyfish`
- `genome_survey.kmer_count.meryl`
- `genome_survey.model.genomescope2`
- `genome_survey.model.smudgeplot`

Current code already prevents full k-mer survey from running on ONT/CLR-only
long reads. It writes either full mode with `clean_short_dna_reads`, a
low-confidence long-read fallback record, or a skip record.

Corrections:

- Keep Jellyfish/Meryl/GenomeScope2/Smudgeplot behind
  `clean_short_dna_reads`.
- Never default full k-mer survey on `clean_long_reads`.
- Clarify whether long-read-only projects should write
  `fallback_low_confidence` or pure skip. If kept, the fallback must be clearly
  a low-confidence prior/coverage note, not a k-mer survey.
- Treat `genome_survey_result` as an assembly-planning prior, not an assembly
  input.

### Assembly

Current registry entries:

- `assembly.candidate.flye`
- `assembly.candidate.hifiasm`
- `assembly.candidate.nextdenovo`
- `assembly.candidate.canu`

Current code still includes `hifiasm` in the automatic ONT/CLR candidate list.
That is the main bug in this audit. `hifiasm` should remain registered for a
HiFi route, but it should not be auto-selected for ONT/CLR-only projects.

Corrections:

- ONT/CLR default candidate set:
  - `assembly.candidate.flye`
  - `assembly.candidate.nextdenovo`
  - `assembly.candidate.canu`
- Keep `assembly.candidate.hifiasm` in the registry, but gate it behind
  `clean_hifi_reads` or an explicit non-ONT/CLR platform route.
- Prefer Flye as the simplest default primary path when no human method decision
  exists.
- Treat NextDenovo as ONT/CLR-compatible, especially for large/complex genomes,
  but require a genome-size prior or human-supplied genome size.
- Treat Canu as a heavier fallback, not a first default for MVP execution.

### Assembly Review

Current entries cover planning for evaluation and checks:

- QUAST
- BUSCO
- Merqury
- mapping coverage through minimap2
- purge_dups
- contamination check through minimap2

The main gap is polishing. `docs/stage_contracts.md` mentions NextPolish, but
no polishing descriptors are registered.

Corrections:

- Add polishing descriptors as planned conditional actions:
  - `assembly_review.polish.nextpolish`
  - optionally `assembly_review.polish.racon`
  - optionally `assembly_review.polish.medaka` for ONT
- Add `samtools` helper descriptors for mapping/coverage pipelines.
- Consider `mosdepth` for stable coverage-depth evaluation.
- Rename or split contamination planning so minimap2 is not presented as a full
  contamination classifier by itself.
- Add BlobTools or BlobToolKit as the future contamination-screen candidate if
  that scope is approved.

Correct flow:

```text
draft_assembly
-> parallel planned evaluation:
   QUAST, BUSCO, mapping coverage, Merqury when short reads exist
-> review gate
-> conditional actions:
   polish, purge_dups, contamination screen, pass
-> final evaluation
-> final_assembly_for_annotation
```

Do not encode numeric thresholds or automatic accept/reject policies in this
task.

### Hi-C Scaffolding

Current candidate methods are reasonable:

- `hic_scaffolding.candidate.yahs`
- `hic_scaffolding.candidate.juicer_3ddna_juicebox`

Current code already gates this stage on Hi-C reads plus chromosome-level
intent, and skips otherwise.

Corrections:

- Keep Hi-C scaffolding optional for ONT/CLR.
- Ensure `run_if` remains:
  - `hic_reads_registered` exists;
  - reviewed final assembly exists;
  - target assembly level is `chromosome`.
- Consider helper descriptors for `bwa` and `samtools` only if the method
  descriptors do not already own those dependencies.
- Preserve pre-Hi-C assembly as the annotation fallback.

### Repeat Annotation

Current planning is mostly aligned:

- EDTA and RepeatModeler are de novo library candidates.
- TRF is an optional tandem-repeat helper.
- RepeatMasker owns masking output.

Corrections:

- Keep EDTA versus RepeatModeler/RepeatMasker as alternative routes unless a
  human-approved descriptor policy chooses one.
- Do not run EDTA and RepeatModeler by default as duplicated library discovery.
- Record the chosen route as provenance when execution is added.

### Gene Annotation

Current planning is mostly aligned with evidence-aware routing:

- BRAKER3, MAKER, AUGUSTUS, Miniprot are candidate methods.
- HISAT2/StringTie, IsoQuant, and GeMoMa are helper/evidence methods.
- IsoQuant is already gated by long RNA / Iso-Seq asset types in code.

Corrections:

- Keep the explicit rule that genome ONT/CLR reads are not IsoQuant input.
- Ensure `isoquant` runs only with `clean_isoseq_reads`,
  `transcript_ccs_reads`, or `raw_isoseq_or_long_rna_reads`.
- Keep mixed-evidence merge policy as a review/human decision.

## Files Likely Touched

- Modify: `docs/stage_contracts.md`
- Modify: `docs/descriptor_spec.md` if internal methods need a formal package
  representation
- Modify: `descriptors/registry.yaml`
- Add: `descriptors/packages/internal.yaml` if approved
- Add: `descriptors/packages/nanoplot.yaml`
- Add: `descriptors/packages/nextpolish.yaml`
- Add optional packages only if approved:
  `racon`, `medaka`, `samtools`, `mosdepth`, `blobtools` or `blobtoolkit`,
  `bwa`
- Add: `descriptors/methods/read_qc/nanoplot_qc.yaml`
- Add: `descriptors/methods/read_qc/make_fofn.yaml`
- Add: `descriptors/methods/assembly_review/nextpolish_polish.yaml`
- Modify: `app/stages/assembly/nodes.py`
- Modify: `app/stages/read_qc/nodes.py` only if internal FOFN descriptors are
  made required by registry validation
- Modify: `app/stages/assembly_review/nodes.py`
- Test: `tests/test_project_scaffold.py`
- Test: `tests/test_read_qc.py`
- Test: `tests/test_assembly.py`
- Test: `tests/test_assembly_review.py`

## Tasks

- [ ] Update the stage/method documentation to distinguish defaults,
  conditional candidates, helpers, and non-default compatibility routes.
- [ ] Decide whether `internal` should be a real descriptor package or whether
  local deterministic helpers stay outside the method registry.
- [ ] Add NanoPlot as an optional read QC report descriptor if approved.
- [ ] Register FOFN generation as `read_qc.prepare.make_fofn` if the internal
  package decision is approved.
- [ ] Add tests proving read QC never chains all length filters by default.
- [ ] Keep genome survey full mode gated on `clean_short_dna_reads`.
- [ ] Clarify long-read-only genome survey fallback versus skip semantics in
  docs and tests.
- [ ] Gate `hifiasm` out of ONT/CLR automatic assembly planning.
- [ ] Update assembly tests so automatic ONT/CLR planning chooses Flye primary
  and NextDenovo/Canu-compatible fallback behavior, not hifiasm.
- [ ] Add assembly-review polishing descriptors, starting with NextPolish if
  the scope is kept narrow.
- [ ] Add `samtools` helper descriptor for mapping/coverage if needed by the
  planned review execution slice.
- [ ] Rename or split minimap2 contamination planning so it is clearly a mapping
  screen, not a complete taxonomy classifier.
- [ ] Keep Hi-C scaffolding optional and gated by Hi-C assets plus chromosome
  target intent.
- [ ] Keep repeat annotation routes alternative, not EDTA plus RepeatModeler by
  default.
- [ ] Add gene annotation tests that genome ONT/CLR reads do not enable
  IsoQuant.

## Acceptance

- Registry and planner docs no longer imply that every method in a stage should
  run.
- Read QC defaults are stats plus FOFN, with optional report and at most one
  approved filter route.
- Full genome survey never runs from ONT/CLR long reads alone.
- ONT/CLR assembly auto-planning does not select hifiasm.
- Polishing is represented as a conditional assembly-review action before real
  polishing execution is attempted.
- Hi-C, repeat, and gene annotation method roles are explicit and evidence-gated.
- No thresholds, purge cutoffs, contact-map policies, annotation merge policies,
  or real execution defaults are invented.

## Non-Goals

- Do not implement real NanoPlot, Flye, NextDenovo, Canu, QUAST, polishing,
  Hi-C, repeat, or gene annotation execution in this task.
- Do not implement requested-stage routing.
- Do not implement RemoteRunner.
- Do not add large dependency installation steps.
- Do not add unapproved biological thresholds.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_project_scaffold tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_assembly_review tests.test_hic_scaffolding tests.test_repeat_annotation tests.test_gene_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
