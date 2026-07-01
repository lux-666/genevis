# 10 Repeat, Gene, And Function Annotation Realization

Task status: partially implemented (repeat-layer slice).

## Goal

Turn downstream annotation from planned contracts into real execution paths from
`final_assembly_for_annotation`.

## Scope

Proceed in order:

1. `repeat_annotation` to produce `softmasked_genome`.
2. `gene_annotation` to perform the gene-structure annotation role and produce
   `genes.gff3`, `transcripts.fa`, and `proteins.fa`.
3. `function_annotation` to perform the functional annotation role and produce
   database-backed function tables.

Start each layer with one narrow method route before adding alternatives.

## Candidate First Routes

- Repeat annotation: `repeatmodeler2_denovo_library`, `repeatmasker_mask`,
  `edta_te_annotation`, and `repeat_landscape`, after choosing one route.
- Gene structure annotation role inside `gene_annotation`:
  `braker3_rnaseq_protein`, `braker2_rnaseq_only`,
  `liftoff_reference_transfer`, `maker_evidence_integration`, and
  `gff3_validate`.
- Functional annotation role inside `function_annotation`: `eggnog_mapper`,
  `interproscan`, `blastp_swissprot`, `kofamscan`, and `go_merge`.

Descriptor names may need a migration from the current registry names. Do not
rename existing descriptors in the same task as real execution unless that
migration is explicitly scoped and tested.

## Boundaries

- Do not skip repeat annotation before gene annotation unless an explicit
  fallback reason exists.
- Do not feed genome ONT/CLR reads to IsoQuant.
- Do not merge RNA/protein/ab initio evidence without an approved merge policy.
- Do not run function annotation without database name, version, snapshot date,
  and input protein provenance.
- Do not rename top-level stages to `gene_structure_annotation` or
  `functional_annotation`; keep the current parent graph stages
  `gene_annotation` and `function_annotation`.
- Do not present merged GO/KEGG/function output as a single true function call;
  preserve evidence source labels such as eggNOG inference, InterPro domain,
  BLAST hit, KEGG KO, and GO term.

## Expected Outputs

- `repeat_library`
- `repeat_annotation_gff`
- `softmasked_genome`
- `gene_models`
- `transcripts.fa`
- `proteins.fa`
- `function_annotation`
- `functional_annotation_tsv`
- `go_terms`
- `kegg_terms`
- `interpro_domains`

## Tasks

- [x] Keep annotation stage names as `repeat_annotation`, `gene_annotation`, and `function_annotation`.
- [x] Add the first explicit local repeat-layer execution route: provided repeat library plus RepeatMasker softmasking.
- [x] Keep default repeat annotation behavior candidate-only unless `stage_outputs.repeat_annotation.execution_mode == "local"` and `primary_method == "repeat_annotation.masking.repeatmasker"` are both explicit.
- [x] Register RepeatMasker softmasked genome, repeat GFF3, command stdout/stderr, bundle artifacts, and ledger attempt events.
- [x] Continue routing gene annotation from `softmasked_genome_ref`, now possibly materialized by local RepeatMasker.
- [x] Keep de novo repeat library discovery, gene-structure annotation execution, function annotation execution, function database downloads, evidence merging, and thresholds deferred.

## Implemented State

- `app/stages/repeat_annotation/nodes.py` preserves existing planning behavior and adds an optional freeze/run/collect branch for local RepeatMasker only with a provided repeat library.
- Local RepeatMasker validates `repeat_annotation.masking.repeatmasker`, writes deterministic `RepeatMasker -lib <repeat_library> -gff -xsmall <assembly>` attempts through `FrozenAttemptSpec`, executes them via `LocalRunner`, and records ledger events.
- Success registers materialized `softmasked_genome.fasta`, `repeat_annotation.gff3`, command stdout/stderr, and the repeat annotation bundle. The stage then advances to `gene_annotation` with real repeat refs.
- Failure records `attempt_failed` and stops at `repeat_annotation`; the workflow does not advance to gene annotation with missing masking outputs.
- De novo repeat libraries remain planned only. Gene annotation and function annotation remain planning/provenance stages in this slice.

改动要点：

- `app/stages/repeat_annotation/builder.py` and `routing.py` insert optional RepeatMasker freeze/run/collect nodes before commit.
- `app/schemas/artifacts.py` defines typed repeat plan, bundle, softmasked genome, hardmasked genome, and GFF3 artifact constants.
- `descriptors/methods/repeat_annotation/repeatmasker_masking.yaml` now documents the provided-library local RepeatMasker route without renaming descriptors.
- `tests/test_repeat_annotation.py` covers fake RepeatMasker success/failure, default candidate-only behavior, local mode without explicit method, and de novo candidate-only behavior.

## Acceptance

- Downstream annotation progresses through repeat, gene, then function
  annotation in order.
- Each real method writes artifacts and ledger events through the existing
  execution boundary.
- Database provenance is required before function annotation completes.
- Evidence-poor gene annotation produces a review warning or human gate for
  annotation evidence selection instead of pretending to be high confidence.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_repeat_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_repeat_annotation tests.test_gene_annotation tests.test_function_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_hic_scaffolding tests.test_repeat_annotation tests.test_gene_annotation tests.test_function_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
