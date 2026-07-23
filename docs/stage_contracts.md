# Stage Contracts

This document defines the current biological inputs, outputs, executable
methods, and review triggers for each workflow stage. Cross-stage file transport
is owned by [`architecture/resource-handoff.md`](architecture/resource-handoff.md).
Registered method identities are owned by `descriptors/registry.yaml`.

## Shared stage shape

Executable stages follow this shape where applicable:

```text
enter -> validate_inputs -> plan_methods -> method_subgraph
      -> collect_outputs -> review_outputs -> commit_stage
```

- validation reads compact state and verified refs only;
- planning selects only methods registered for that stage;
- method execution uses the shared freeze/run/collect lifecycle;
- collection publishes artifacts and compact evidence refs;
- review may open a human gate;
- commit updates the central logical resource table and advances routing.

Unregistered future tools are not stage candidates. Adding one requires its
descriptor, executable adapter, routing, run-config contract, and tests in the
same change.

## 1. Inventory

Purpose: register manifest assets without transforming them.

Inputs:

- manifest files and directories;
- optional sample, platform, assembly-level, and genome-size metadata.

Current behavior:

- validates and records supplied assets;
- publishes the asset registry;
- initializes `long_reads`, `short_reads`, `hic_reads`, and `assembly` logical
  resources when the corresponding assets exist;
- routes an existing assembly past read QC, survey, and assembly when those
  stages are not needed.

Ambiguous long DNA/RNA classification opens a review gate. Inventory does not
run QC, survey, or assembly tools.

## 2. Read QC

Purpose: inspect long reads, optionally apply an approved length filter, and
prepare tool-local handoff artifacts.

Logical input and output:

- requires `long_reads`;
- updates `long_reads` after an approved filter.

Registered methods:

- `read_qc.long_read_stats.seqkit_stats`;
- `read_qc.length_filter.skip`;
- `read_qc.length_filter.seqkit_seq`.

SeqKit statistics always describe the selected long-read resource. Length
filtering is optional and requires an explicit threshold/approval. A generated
FOFN is an internal artifact; downstream stages consume the canonical path list.

Review is required when filtering would discard excessive yield or when the
input cannot be validated as readable long-read sequence data.

## 3. Genome Survey

Purpose: estimate genome size and related k-mer metrics.

Logical inputs:

- prefers `short_reads` when present;
- otherwise uses `long_reads`.

Registered methods:

- `genome_survey.kmer_count.jellyfish`;
- `genome_survey.model.genomescope2`.

The stage publishes immutable survey evidence and compact scalar observations;
it does not create a cross-stage path resource. An explicit operator genome size
has priority over a survey estimate. If neither an explicit value nor a valid
estimate exists when assembly requires one, the workflow fails with
`GENOME_SIZE_UNAVAILABLE`.

## 4. Assembly

Purpose: build an auditable assembly candidate from long reads.

Logical inputs and output:

- requires `long_reads`;
- consumes an explicit or survey-derived genome size when required;
- may use registered Hi-C/RNA-seq presence as parameter-recommendation context;
- publishes `assembly`.

Registered methods:

- `assembly.candidate.flye`;
- `assembly.candidate.hifiasm`;
- `assembly.candidate.hifiasm_ont`;
- `assembly.candidate.nextdenovo`.

Method compatibility is validated against the selected read type and run
configuration. Assembly, AutoHiC, and T2T parameter recommendations occur only
after method selection and require human approval before freezing an attempt.
Each recommendation is an attempt-local sparse mapping; retries do not inherit
omitted values or route around approval through model output.
Operator-directed Flye reruns return through the same direct-attempt lifecycle.

Missing required parameters, incompatible read types, execution failure, and
missing expected outputs are explicit failures rather than planning success.

## 5. Assembly Dedup

Purpose: create conservative duplicate-purging evidence while retaining the raw
assembly candidate.

Logical inputs and output:

- requires `assembly` and `long_reads`;
- updates `assembly` with the retained candidate relation.

Registered method:

- `assembly_dedup.purge_dups`.

The stage materializes depth, cutoff, self-alignment, duplicate-region, and
purged-sequence evidence. It preserves the raw candidate so Assembly Review can
compare or retain it. Missing materialized inputs are represented explicitly;
the stage does not synthesize a successful purge.

## 6. Assembly Review

Purpose: evaluate assembly candidates and adopt the assembly used downstream.

Logical input and output:

- requires `assembly`;
- updates `assembly` after review or an approved corrective action.

Registered methods:

- `assembly_review.eval.quast`;
- `assembly_review.eval.busco`.

QUAST and BUSCO evidence is collected through an ordered shared lifecycle.
BUSCO runs only with operator-provided offline lineage provenance. Review may
accept a candidate, request supported polish/purge actions, schedule an
operator-parameterized Flye rerun, or stop the workflow. Decisions are bound to
the current review request and candidate refs.

Signals such as duplicated BUSCO content, size inflation, abnormal depth, or
missing/inconsistent evidence prevent unsupported automatic acceptance.

## 7. Hi-C Scaffolding

Purpose: produce and review a chromosome-scale AutoHiC assembly.

Logical inputs and output:

- requires `assembly` and, when entered, `hic_reads`;
- updates `assembly` only after the adoption decision.

Registered method:

- `hic_scaffolding.candidate.autohic`.

The parent graph bypasses this stage when chromosome-scale assembly is not
requested or required inputs are absent. AutoHiC owns the Juicer/3D-DNA and
model-assisted correction pipeline. Its adjustable L2 surface includes the
restriction enzyme, edit rounds, correction switches, and error thresholds.
Every shape-valid sparse recommendation requires parameter approval; successful
execution then opens an adoption gate. The operator may adopt the chromosome
assembly, retain the previous assembly, or request a fresh parameter attempt.
Execution-failure retries require explicit operator policy and retry budget.

## 8. T2T Finishing

Purpose: inspect terminal telomere evidence and conservatively extend supported
assembly ends without overwriting the input FASTA.

Logical inputs and output:

- requires `assembly`;
- consumes the complete ordered `ont_read_refs` list when supplied, preferring
  all clean long-read assets over raw assets;
- updates `assembly` only when a materialized result is selected.

Registered method:

- `t2t.alignment.minimap2`.

The stage obtains a sparse, human-approved threshold recommendation before
scanning or minimap2 execution. It records source-attributed telomere scans, an
immutable registered PAF, attempt/execution-bound candidate evidence,
junction-aware patch records, and a finishing report. Reverse-strand tails are
oriented target-forward; non-zero junction distances trim the target before
extension. Ambiguous, chimeric, overlapping, or conflicting evidence is never
silently materialized, and the source FASTA is never overwritten.

## 9. Repeat Annotation

Purpose: mask repeats on the adopted assembly for downstream gene prediction.

Logical input and output:

- requires `assembly`;
- publishes `masked_assembly`.

Registered method:

- `repeat_annotation.masking.repeatmasker`.

Execution requires an explicitly registered and trusted repeat library.
RepeatMasker publishes masking output and annotation evidence. The stage does
not claim that unregistered de-novo repeat tools ran.

## 10. Gene Annotation

Purpose: produce reviewed gene models and predicted proteins.

Logical inputs and output:

- prefers `masked_assembly`;
- may fall back to `assembly` only with an explicit unmasked-fallback reason;
- publishes `predicted_proteins`.

Registered selectable methods:

- `gene_annotation.candidate.augustus`;
- `gene_annotation.candidate.miniprot`.

Current helper executions are `gene_annotation.materialize.gffread`,
`gene_annotation.merge.evm`, and `gene_annotation.busco`. Helpers are not
operator-selectable stage methods.

When gene models and predicted proteins are supplied in the manifest, the stage
adopts those exact assets and records their provenance instead of running a
predictor. Otherwise it executes the selected registered predictors, reviews
their evidence, optionally merges retained candidates, materializes proteins,
and records gene-set completeness. Evidence conflicts and unmasked fallback are
review signals, not implicit method-selection rules.

## 11. Function Annotation

Purpose: attach database-backed homology annotations to predicted proteins.

Logical input:

- requires `predicted_proteins`.

Registered method:

- `function_annotation.candidate.diamond`.

DIAMOND execution requires explicit database provenance whose input-protein ref
matches the materialized proteins. The stage publishes annotation artifacts and
terminal evidence; it does not create another cross-stage path resource.
Unknown database versions, missing provenance, execution failure, or missing
output prevent successful completion.
