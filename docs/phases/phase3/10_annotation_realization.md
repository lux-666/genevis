# 10 Repeat, Gene, And Function Annotation Realization

Task status: planned.

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

## Acceptance

- Downstream annotation progresses through repeat, gene, then function
  annotation in order.
- Each real method writes artifacts and ledger events through the existing
  execution boundary.
- Database provenance is required before function annotation completes.
- Evidence-poor gene annotation produces a review warning or human gate for
  annotation evidence selection instead of pretending to be high confidence.
