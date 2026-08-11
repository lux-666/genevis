# Stage Contract Reference

> **Status:** implemented for current stage-local contracts; the accepted
> GapCloser and Base Polish handoffs remain planned placeholder capabilities.
> **Read when:** exact biological stage inputs, outputs, methods, or review
> triggers matter.  
> **Comes from:** [Runtime orchestration](../topics/runtime-orchestration.md).  
> **Feeds into:** resource validation, method planning, execution, and review.  
> **Related contracts:** [Resource handoff](resource-handoff.md),
> [Descriptors](descriptors.md),
> [Execution lifecycle](execution-lifecycle.md).  
> **Code evidence:** `app/workflow.py`, `app/graph/builder.py`,
> `app/stages/`, `descriptors/registry.yaml`.

This document defines the current biological inputs, outputs, executable
methods, and review triggers for each workflow stage. Cross-stage file transport
is owned by [`resource-handoff.md`](resource-handoff.md).
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

The frozen occurrence sequence decides which stage subgraphs are reached.  It
does not prove that their biological inputs exist: every reached stage resolves
and validates its own logical resources before it plans or runs a method.

Review is an optional, reusable stage occurrence. Assembly-producing and
refining stages do not create a mandatory Review boundary. When
`assembly_review` is explicitly present, it carries an exact `review_target`
with `resource_type: "assembly"` and the nearest earlier handoff producer's
`producer_step_id`; freeze never inserts a missing Review. Thus
`assembly -> hic_scaffolding -> assembly_review` reviews the Hi-C delivery,
while `t2t` may terminate or continue to Base Polishing without Review.

The canonical complete-genome recommendation is:

```text
read_qc -> genome_survey -> assembly -> assembly_dedup -> assembly_review
        -> hic_scaffolding -> gapcloser -> t2t -> base_polish
        -> assembly_review -> done
```

This recommendation fixes the intended biological handoff order without making
the entire list mandatory. Its first Review evaluates the retained assembly
after Dedup. Its final Review reserves the position that will evaluate the
polished assembly once Base Polish implements its handoff; today that Review is
unreachable and its frozen target remains T2T. `done` is an implicit parent
terminal, not a requestable stage.

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
- registers an `assembly_fasta_registered` manifest asset as the central
  `assembly` logical resource.

An existing assembly does not rewrite or constrain the frozen occurrence
sequence. `assembly_review` is the normal recommended first analytical
occurrence for that input, not a public-run entry restriction.

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
filtering is optional and runs only when the frozen stage request already
contains a positive `min_len` and an explicit approval flag. There is no
stage-local Read QC review gate or implemented automatic excessive-yield-loss
threshold; absent or unapproved thresholds select the registered skip behavior.
A generated FOFN is an internal artifact; downstream stages consume the
canonical path list. Missing or unusable inputs and execution failures terminate
the stage rather than opening a Read QC review gate.

## 3. Genome Survey

Purpose: estimate genome size and related k-mer metrics.

Logical inputs:

- a full k-mer survey requires at least one usable `short_reads` or
  `long_reads` resource;
- selects `short_reads` before `long_reads`; the canonical logical-resource
  resolver owns clean/raw lifecycle priority within either family;
- with no usable reads, an explicit operator genome size permits the
  `skipped_no_usable_reads` evidence path; without either, the stage fails.

Registered methods:

- `genome_survey.kmer_count.jellyfish`;
- `genome_survey.model.genomescope2`.

The stage publishes immutable survey evidence and compact scalar observations;
it does not create a cross-stage path resource. An explicit operator genome size
has priority over a survey estimate. If neither an explicit value nor a valid
estimate exists when assembly requires one, the workflow fails with
`GENOME_SIZE_UNAVAILABLE`. The checkpoint's `survey_input_family` and
`resolved_assets` expose only the `short_reads` and `long_reads` logical
families. Jellyfish publishes typed k-mer `ArtifactRef` objects, which
GenomeScope2 consumes it without a path-string conversion.

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

Assembly Review is a reusable, independent subgraph. Inventory registers an
existing assembly through the same central `assembly` `ResourceRef` used by
upstream-produced assemblies. When an Assembly Review occurrence is reached,
the stage validates the assembly and the candidate/evidence required by its
current operation; it must not bypass the resource contract with a bare
`fasta_path`. The same contract is reusable after any occurrence that owns the
central assembly handoff, including Assembly, Dedup, Hi-C, T2T, or a previous
Review action, whether or not the selected bytes changed. Before it loads
candidates, it verifies that the central assembly
`ResourceRef.source_stage` and `.workflow_step_id` match its frozen
`review_target`; a stage name alone cannot satisfy the handoff. A repeated
occurrence starts with a fresh copy of the same immutable stage-scoped seed,
not occurrence-specific method configuration, and remains separate by
step-scoped artifacts, attempts, review records, and Ledger facts.

Registered methods:

- `assembly_review.eval.quast`;
- `assembly_review.eval.busco`.

QUAST and BUSCO evidence is collected through an ordered shared lifecycle.
BUSCO runs only with operator-provided offline lineage provenance. Review may
accept a candidate, adopt a separately completed polish/purge action, or stop
the workflow. Decisions are bound to the current review request and typed
candidate refs.

Signals such as duplicated BUSCO content, size inflation, abnormal depth, or
missing/inconsistent evidence prevent unsupported automatic acceptance.

## 7. Hi-C Scaffolding

Purpose: produce and review a chromosome-scale AutoHiC assembly.

Logical inputs and output:

- requires `assembly` and, when entered, `hic_reads`;
- republishes the central `assembly` `ResourceRef` after an adoption decision;
  its bytes and candidate relation change only when a scaffold is adopted.

Registered method:

- `hic_scaffolding.candidate.autohic`.

This stage runs only when its frozen workflow occurrence is reached. Missing
assembly, paired Hi-C reads, or runtime configuration then produces a terminal
`blocked_missing_requirements` outcome. AutoHiC owns the Juicer/3D-DNA and
model-assisted correction pipeline. Its adjustable L2 surface includes the
restriction enzyme, edit rounds, correction switches, and error thresholds.
Every shape-valid sparse recommendation requires parameter approval; successful
execution then opens an adoption gate. The operator may adopt the chromosome
assembly, retain the previous assembly, or request a fresh parameter attempt.
Execution-failure retries require explicit operator policy and retry budget.

An accepted scaffolding adoption verifies the current assembly candidate and
the adopted primary artifact, then publishes a derived executed candidate in
the active Hi-C occurrence before replacing the central resource. The immutable
adoption-decision artifact identifies that derived candidate. Retaining the
prior assembly keeps the prior bytes and candidate relation, but republishes the
central `assembly` `ResourceRef` under the active Hi-C occurrence.

Freeze-time readiness validation covers inputs, local tools, models, and the
declared output locations; it does not preflight files that AutoHiC has not yet
generated. After a successful process result and before the first new
publication, the collector requires every frozen `expected_outputs` member
(FASTA, `.hic`, `.assembly`, HTML) to be a file, or to have a matching
attempt-local indexed publication recoverable during crash replay. Any missing
member without that record terminates the stage with `failure_kind: collect`,
without a review candidate, adoption gate, review bundle, or review-product
publication. Publication is non-transactional: a later storage failure can
leave an earlier product indexed as an orphan. That orphan has neither
`attempt_succeeded` nor `artifact_registered` semantic ledger evidence, does
not create a review gate, and does not cause automatic retry or rollback.

## 8. GapCloser Placeholder

Intended purpose: fill structural sequence gaps in an assembly when an operator
places this placeholder occurrence in the frozen workflow.

Accepted placement and handoff design:

- the complete recommendation places `gapcloser` after `hic_scaffolding` and
  before `t2t`;
- the implemented stage will require the current central `assembly`;
- successful adoption will publish an occurrence-owned assembly candidate and
  replace the central `assembly` handoff consumed by T2T or a later Review.

Current contract:

- `gapcloser` is wired into the parent graph but has no registered method,
  descriptor, local-execution configuration, artifact contract, or resource
  handoff;
- the complete-genome and manifest-derived recommendations include its intended
  position with local execution disabled, while generic prefix expansion omits
  it;
- a frozen occurrence writes only a strict `blocked_not_implemented` cursor and
  terminates through the existing failed boundary;
- an absent occurrence has no runtime behavior. `None` is only an unfinished
  planner/TUI draft, and public `gene-agent run` rejects JSON `null` before freeze.

This placeholder does not read or update the central `assembly` resource. Tool
selection, input reads, expected products, validation, adoption, and provenance
remain future contracts.

## 9. T2T Finishing

Purpose: inspect terminal telomere evidence and conservatively extend supported
assembly ends without overwriting the input FASTA.

Logical inputs and output:

- requires the current adopted `assembly` and `long_reads`;
- consumes the complete ordered, platform-preserving long-read set, including
  ONT, PacBio HiFi, CLR, and CycloneSEQ inputs;
- republishes the central `assembly` `ResourceRef` after review; its bytes and
  candidate relation change only when a materialized patch is selected.

T2T consumes the central assembly as it exists when that frozen occurrence is
reached. It never consumes `hic_reads` directly. A configured
`t2t.assembly_fasta_ref` is only an equality assertion against the current
central assembly; it cannot bypass resource adoption by an earlier occurrence.

Registered method:

- `t2t.alignment.minimap2`.

Before threshold recommendation or scanning, T2T resolves one telomere motif
from a schema-3 snapshot of canonical supplied species facts. A supplied
`species_facts.telomere_motif` is validated and recorded directly through the
motif-resolution evidence chain; otherwise the persisted LLM boundary resolves
it from the supplied scientific name. Missing identity, unresolved model
output, and pre-v3 motif/config checkpoint fields fail closed; none supplies
motif evidence, and no common repeat is assumed. The verified resolution value,
source, and ref are bound into the schema-3 threshold-recommendation snapshot
and are the only motif evidence accepted by later scans and patch checks. The
stage then obtains a sparse, human-approved threshold recommendation before
scanning or minimap2 execution. Reads are grouped by platform-specific minimap2
preset inside one frozen attempt and produce one combined immutable PAF. The
approved support threshold is applied exactly; the adapter adds no hidden
minimum or junction-spread veto. It records source-attributed telomere scans,
attempt/execution-bound candidate evidence, junction-aware patch records, and a
finishing report. `auto_patch` first creates and validates a candidate, then
opens a separate human adoption gate; the shared `assembly` resource is not
changed until adoption. Reverse-strand tails are oriented target-forward;
non-zero junction distances trim the target before extension. Ambiguous,
chimeric, overlapping, or conflicting evidence is never silently materialized,
and the source FASTA is never overwritten.

When an adopted patch changes the assembly, T2T verifies the current candidate
and exact patched primary artifact, then publishes a derived executed candidate
bound to the immutable adoption-decision artifact before replacing the central
resource. It retains the predecessor candidate and primary output as lineage.
Keeping the prior assembly keeps its bytes and candidate relation, but
republishes the central `assembly` `ResourceRef` under the active T2T
occurrence.

## 10. Base Polish Placeholder

Intended purpose: correct base substitutions and small indels in the assembly
available when an operator places this placeholder occurrence in the frozen
workflow.

Accepted placement and handoff design:

- the complete recommendation places `base_polish` after `t2t`;
- the implemented stage will require the current central `assembly` plus the
  polishing evidence required by its selected method;
- successful adoption will publish an occurrence-owned polished assembly as the
  central handoff;
- the following finish `assembly_review` occurrence will bind to that exact Base
  Polish producer step.

Current contract:

- `base_polish` is wired into the parent graph but has no registered method,
  descriptor, local-execution configuration, artifact contract, or resource
  handoff;
- the complete-genome and manifest-derived recommendations include its intended
  position with local execution disabled, while generic prefix expansion omits
  it;
- a frozen occurrence writes only a strict `blocked_not_implemented` cursor and
  terminates through the existing failed boundary;
- an absent occurrence has no runtime behavior. `None` is only an unfinished
  planner/TUI draft, and public `gene-agent run` rejects JSON `null` before freeze.

This placeholder does not read or update the central `assembly` resource.
Polishing-read choice, method selection, expected products, validation,
adoption, and provenance remain future contracts.
