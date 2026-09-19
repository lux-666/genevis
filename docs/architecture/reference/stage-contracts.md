# Stage Contract Reference

> **Status:** accepted and implemented for current stages. Base
> Polish remains a planned placeholder capability.
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
- fixed-method stages receive a roster containing only their registry-default
  methods; Assembly may omit `selected_method_names` for stage-owned deferred
  binding (omission is not an empty roster);
- planning resolves applicability and parameters without rewriting a fixed
  roster; deferred Assembly first recommends/confirms its method from upstream
  QC/survey evidence and then binds one validated `primary_method`;
- method execution uses the shared freeze/run/collect lifecycle;
- collection publishes artifacts and compact evidence refs;
- review may open a human gate;
- commit publishes the central logical-resource projection where applicable
  and returns the occurrence handoff to the parent; it does not advance the
  frozen workflow by implication.

Only when an occurrence settles does its stage return an explicit
`workflow_step_outcome` bound to the active frozen occurrence. `completed`
means the stage delivered its contract; `skipped` is permitted only for a
stage-specific no-delivery path defined below; `failed` means the contract was
not delivered. A subgraph stop is not necessarily settlement: `blocked_human`
is a recoverable pause on the still-active occurrence and returns no occurrence
outcome. A method disposition, attempt result, private cursor status, or route
marker never implies an occurrence outcome. The shared occurrence-settlement
boundary, invoked by both the parent controller and terminal recovery, validates
the explicit handoff, proves exact coverage of the effective method roster,
records any immutable delivery, and writes the single occurrence terminal. For
deferred Assembly, settlement uses the validated stage-owned method binding as
the effective roster and requires its method disposition.

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

Accepted behavior:

- receives a frozen manifest whose declared biological members have already
  passed the regular-file and logical/decompressed non-empty checks owned by
  the [run-data contract](data-contract.md#49-declared-input-content-is-logically-non-empty);
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

The accepted Read QC fact package is closed and recomputable:

- the selected ordered read members are frozen as input dependencies before
  execution; registry members retain asset IDs plus the registry ref, while
  run-owned members retain complete `ArtifactRef` values;
- per-file SeqKit statistics are diagnostic and retain those exact ordered
  source identities;
- every run, including a one-file run, publishes one immutable
  whole-read-set aggregate artifact computed directly from the original read
  stream, not by merging or weighting rounded per-file rows;
- the aggregate owns read count, total bases, minimum, maximum, arithmetic
  mean length, whole-set N50, Q20-base percentage, Q30-base percentage, and
  average quality. FASTA quality fields are explicitly not applicable rather
  than zero. A FASTQ row with zero sequences and zero bases has canonical
  `Q20=0.0`, `Q30=0.0`, and `AvgQual=0.0`, whether that SeqKit version renders
  those cells as zero or empty. The SeqKit parser applies this rule identically
  to per-file and whole-set rows before invariant comparison;
- the aggregate depends directly on the frozen read members. It must not claim
  that per-file statistics are its source. A summary that contains only
  aggregate metrics depends on the aggregate; if it embeds per-file
  diagnostics it lists both refs;
- a filtered read resource publishes an immutable read-set manifest relating
  every derived member to its source asset/ref and approved filter parameters.

An approved filter that retains no reads may complete its executable method and
publish a zero-valued aggregate. The Read QC occurrence nevertheless fails with
`no_reads_retained` and does not replace `long_reads` with empty files. This is
distinct from a declared empty input, which is rejected before freeze.

## 3. Genome Survey

Purpose: estimate genome size and related k-mer metrics.

Logical inputs:

- a full k-mer survey requires at least one usable `short_reads` or
  `long_reads` resource;
- selects `short_reads` before `long_reads`; the canonical logical-resource
  resolver owns clean/raw lifecycle priority within either family;
- with no usable reads, an explicit operator genome size permits the
  `skipped_no_usable_reads` evidence path; without either, the stage fails.

Input completeness is declaration-owned. A manifest containing long reads but
no short reads is a valid, intentional long-read-only survey input; the stage
does not guess that the user omitted a file. When both families are declared,
short reads take priority. A supplied `genome_size_estimate` remains an
operator prior; its exact precedence over measurements is owned by the
[Shared Biological Facts Contract](shared-facts.md#estimated_genome_size).

Registered methods:

- `genome_survey.kmer_count.jellyfish`;
- `genome_survey.model.genomescope2`.

The stage publishes immutable survey evidence and compact scalar observations;
it does not create a cross-stage path resource. If neither an explicit value nor
a valid estimate exists when assembly requires one, the workflow fails with
`GENOME_SIZE_UNAVAILABLE`. The checkpoint's `survey_input_family` and
`resolved_assets` expose only the `short_reads` and `long_reads` logical
families. Each resolved read retains the registry's frozen `compression` fact;
Jellyfish consumes `none`/`unknown` inputs directly and streams `gzip` members
through `gzip -cd` without requiring an operator-created decompressed copy.
Other declared compression codecs fail manifest validation until they have an
explicit execution contract. Filename suffixes are not compression authority.
GenomeScope2 receives the frozen `shared_parameters.species_facts.ploidy`, or
diploid `2` when it is absent. Long-read-only k-mer estimates are recorded with
low confidence; short-read estimates are high confidence. Jellyfish publishes
typed k-mer `ArtifactRef` objects, which GenomeScope2 consumes without a
path-string conversion.

Because the frozen Jellyfish script invokes both commands for gzip inputs, its
selected machine-method mapping must declare `jellyfish` and `gzip` in the
probed tool closure. The active machine mapping is frozen through the
[whole-run readiness boundary](execution-lifecycle.md#whole-run-readiness-boundary);
the committed `envs.yaml.example` is only a bootstrap seed.

Outcome rules are explicit. With usable reads and local execution enabled, both
methods must complete before the stage completes. With usable reads but local
execution disabled, an explicit operator genome size may produce a completed
`provided_only` result while both methods are `skipped`; without that supplied
value the stage fails. With no usable reads and an explicit size, the stage uses
its defined `stage_skipped` path. A durable `GenomeSurveyResult.status` must
agree with the occurrence outcome and is never left `planned` after settlement.

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

Method compatibility is validated against the selected read type and upstream
evidence. The recommendation payload carries explicit upstream-evidence status;
a field whose producer did not complete is null and must not be treated as an
observation. Assembly's route/method decision is stage-local and follows the
frozen [recommendation policy](recommendation-policy.md). In advisor mode, the
advisor recommends a method from the input-compatible frozen candidate set,
the stage persists separate immutable model-request, model-response, and
parsed-recommendation artifacts, and human confirmation binds
`assembly_selection`. In deterministic-baseline mode, the stage skips the model
and fails closed when route/platform evidence cannot determine a compatible
default method. A crash after advisor-response publication replays that
response by request hash instead of calling the model again. Assembly, AutoHiC,
and T2T parameter
recommendations occur only after method selection and require human approval
before freezing an attempt; the frozen parameter policy determines whether that
recommendation is model-proposed or an empty-L2 deterministic baseline. The
dynamic Assembly method must resolve through
the frozen dependency snapshot and pass stage-time preflight; it is not read
from live environment configuration.
The exact fact placement is owned by the
[run-data contract](data-contract.md#101-assembly-method-recommendation).
Each recommendation is an attempt-local sparse mapping; retries do not inherit
omitted values or route around approval through model output.
`assembly.threads` is the method-independent RunConfig execution resource and
is frozen into the selected method's L0 execution contract. For Hifiasm and
Hifiasm-ONT, a supplied `species_facts.ploidy` binds `--n-hap` as an L1 fact;
when absent, the catalog retains its diploid default of `2`.

Missing required parameters, incompatible read types, execution failure, and
missing expected outputs are explicit failures rather than planning success.
Local execution disabled is not successful Assembly planning: unless the
active occurrence can deliver an already materialized, contract-valid candidate
through a separately defined reuse path, Assembly fails with no delivery and
its selected method is `skipped`. A planned candidate bundle alone never
supports `stage_completed`.

## 5. Assembly Dedup

Purpose: create conservative duplicate-purging evidence while retaining the raw
assembly candidate.

Logical inputs and output:

- requires `assembly` and `long_reads`;
- updates `assembly` with the retained candidate relation.

Registered method:

- `assembly_dedup.purge_dups`.

When both local execution and `assembly_dedup.automatic_purge_approved` are
frozen in RunConfig, the stage materializes depth, cutoff, self-alignment,
duplicate-region, and purged-sequence evidence. It preserves the raw candidate
so Assembly Review can compare or retain it. Without explicit automatic-purge
approval, the stage safely passes the original candidate through. Missing
materialized inputs are represented explicitly; the stage does not synthesize
a successful purge.

The current approval authorizes the complete automatic `calcuts` path; there is
no separate operator gate for inspecting the depth histogram or derived cutoff
values before purge execution. Such a cutoff-review gate remains a design
candidate rather than a current requirement. Preserving the raw candidate and
comparing raw/purged candidates in Assembly Review are the implemented
safeguards.

The authoritative outcome table is:

| Input candidate | Local execution | Automatic purge approved | Occurrence outcome | Stage delivery | `purge_dups` disposition | Assembly delivery |
|---|---|---|---|---|---|---|
| materialized | authorized | yes; succeeds | `completed` | `executed` | `completed` | `materialized`, new purged candidate |
| materialized | authorized | no | `completed` | `pass_through` | `skipped` with `automatic_purge_not_approved` | `reused`, original candidate |
| materialized | not authorized | any | `completed` | `pass_through` | `skipped` with `local_execution_not_enabled` | `reused`, original candidate |
| absent or non-materialized | any | any | `failed` | none | `not_started` with `no_materialized_assembly` | none |
| materialized | authorized | yes; execution or collection fails | `failed` | none | `failed` with cause | none |

Pass-through is deliberately not `stage_skipped`: the Dedup occurrence delivers
a verified assembly relation, even though the method does not run. Conversely,
missing input is not a failed purge attempt because no attempt started. The
checkpoint's `purge_dups_status` is only a projection of the occurrence-bound
method-disposition fact.

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
candidates, it verifies the producer's canonical completed terminal and unique
immutable `resource_delivered` fact, then requires the central `ResourceRef`
candidate, primary output, path, source stage, and step to match that fact and
its frozen `review_target`. Mutable source labels or a stage name alone cannot
satisfy the handoff. A repeated
occurrence starts with a fresh copy of the same immutable stage-scoped seed,
not occurrence-specific method configuration, and remains separate by
step-scoped artifacts, attempts, review records, and Ledger facts.

Registered methods:

- `assembly_review.eval.quast`;
- `assembly_review.eval.busco`.

QUAST and BUSCO evidence is collected through an ordered shared lifecycle.
QUAST runs in reference-free mode by default. A manifest may register exactly
one `reference_genome_fasta`; Inventory exposes it as the `reference_genome`
logical resource, and every QUAST attempt binds its registry asset identity,
size, and checksum before passing it through `-r`. The per-run reference FASTA
is independent of the host-owned NCBI snapshot registry and is frozen with the
session inputs.
BUSCO and NCBI reference data resolve only from the host database registry
captured in the frozen dependency manifest. Assembly Review requires
`input_manifest.user_metadata.target_species` as an NCBI Latin scientific name
(for example, `Solea ovata`). It is the only accepted run-owned species input:
common names, LLM-derived names, alternate metadata keys, and unspecified names
such as `Genus sp.` do not participate in resolution. Missing or malformed
values fail input preparation with the manifest field path; unresolved names
fail database readiness against the configured NCBI snapshots.

The host registry owns one `databases.ncbi.reference_dir`. Each immediate child
is one same-prefix `*.ncbi.json`, `*.ncbi.lineage`, `*.ncbi.lineage2`, and
`*.ncbi.tsv` snapshot. Readiness searches the target scientific name across all
installed children using the existing exact, same-genus, same-family, then
same-order fallback. The full lineage supplies matching terms and `lineage2`
supplies ranked taxonomic hints. Selection uses the best available tier; equal
tiers prefer more primary records and then the snapshot directory name.
Readiness fails only when no child provides useful evidence. BUSCO selection
first applies one deterministic taxonomy rule to the selected, verified
snapshot: when the full `*.ncbi.lineage` taxonomy for the exact target taxid,
or the common full taxonomy of all same-genus rows when no exact target exists,
contains `Viridiplantae` or `Embryophyta`, the runtime requires and selects the
installed `embryophyta_odb10` lineage. `lineage2` remains limited to ranked
taxonomic hints.
This rule does not infer taxonomy from snapshot directory names,
related-species `Annotation BUSCO Lineage` values, or the host default. A plant
target blocks before freeze when that fixed lineage is not installed.

When the fixed plant rule does not apply, the selected snapshot maps the
scientific name to related-species `Annotation BUSCO Lineage` values. The
runtime intersects those annotations with the host registry and requires
exactly one compatible installed lineage before freeze. The fixed plant rule
takes precedence over a narrower plant lineage annotation. Snapshot
creation/installation owns producing and validating the reference data; the
user does not inspect snapshot rows or choose a lineage.
`assembly_review.busco.lineage` is not a public selection surface. When present
in RunConfig, it is only a consistency assertion; a mismatch with the
species-derived lineage fails with the RunConfig field path. Templates and TUI
intake do not create it. RunConfig never owns database paths,
versions, or provenance, and the host `default_lineage` is not a fallback for
an unresolved Assembly Review species. Review may accept a candidate, adopt a
separately completed polish/purge action, or stop the workflow. Decisions are
bound to the current review request and typed candidate refs.

NCBI comparative evidence and statistics verify and parse the exact same
snapshot bytes. Dependency freeze records the combined four-file identity for
every installed child snapshot. Target resolution and stable source-failure
classification are shared; first-useful-tier
evidence selection and all-tier statistical cohort construction remain separate
stage contracts.

Target species is manifest-owned metadata. Inventory projects the same frozen
Latin scientific name into shared biological facts for Assembly method and
parameter recommendation, and rejects a conflicting RunConfig copy. An absent,
malformed, or unresolved name, an ambiguous BUSCO mapping, or a lineage
unavailable in the host registry blocks Assembly Review before execution; the
system never invents a target, asks an LLM to resolve a common name, or falls
back to an unrelated global lineage.

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
assembly, paired Hi-C reads, or runtime configuration sets the private Hi-C
cursor to `blocked_missing_requirements`, but the explicit occurrence outcome is
`failed`; parent settlement therefore reaches `failed_terminal` without an
`active_interrupt`. The cursor label is diagnostic and is not a resumable or
terminal outcome. AutoHiC owns the Juicer/3D-DNA and model-assisted correction
pipeline. Its adjustable L2 surface includes the restriction enzyme, edit
rounds, correction switches, and error thresholds. Every shape-valid sparse
recommendation requires parameter approval; successful execution then opens an
adoption gate. Those immutable parameter-approval and adoption gates, and only
those gates, may pause the active occurrence as `blocked_human`. The operator
may adopt the chromosome assembly, retain the previous assembly, or request a
fresh parameter attempt. Execution-failure retries require explicit operator
policy and retry budget.

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

## Shared placeholder boundary

Base Polish is a parent-graph placeholder. It has no registered
method, descriptor, local-execution configuration, artifact contract, or
resource handoff. A requested occurrence writes `blocked_not_implemented` and
terminates through the failed boundary; an absent occurrence has no runtime
behavior. It neither reads nor updates the central `assembly` resource.

## 8. GapCloser

Purpose: fill internal N-gaps with TGS-GapCloser using explicitly classified
PacBio HiFi reads. The first slice supports only the no-error-correction route
(`--ne --tgstype pb --minmap_arg '-x asm20'`). ONT, CLR, CycloneSEQ, unknown
platforms, and mixed-platform current read sets are rejected, not reclassified.
This stage does not infer that length-filtered noisy reads are corrected reads.

The registered method is `gapcloser.tgs_gapcloser`. It consumes the current
verified assembly candidate and complete current HiFi read set through the
existing resource resolver. FASTA/FASTQ (plain or gzip) reads are validated and
normalized to a run-owned FASTA; source identities remain in an indexed input
record. The original assembly and reads are never overwritten. Each occurrence
uses its own tool working directory because upstream uses fixed checkpoint tags.

RunConfig carries bounded operator settings, not an LLM recommender. Execution
uses the shared freeze/run/collect lifecycle. Both native 1.2.1 products,
`.scaff_seqs` and `.gap_fill_detail`, are mandatory (the upstream README uses
different suffixes; the adapter follows
[the verified upstream source](https://github.com/BGI-Qingdao/TGS-GapCloser/tree/e1a7e01c6259e6651a8007e4e4c3b887bef38b19)).
Collection validates scaffold identity,
segment coordinates, copied source sequence and N segments, and publishes an
input/output gap-count and length report. Those checks prove structural
consistency, not consensus accuracy or biological improvement.
Scaffolds without internal gaps must remain unchanged. Unfilled N segments
consume the same source Ns; source prefixes and suffixes cannot be discarded.
Inserted sequence and overlap closures (including adjacent S segments without
an F segment) must bridge exactly one internal source gap between retained
anchors. Only that gap's flanks may be trimmed, with removed bases reported.

A separate manual-only adoption gate offers `accept_candidate`,
`keep_prior_assembly`, or `stop_human_review`. No timeout auto-adopts a patch.
Only a verified adoption replaces the central assembly; keeping the prior
assembly declares a reused delivery. Missing local-execution authorization fails closed.
An assembly without internal gaps reuses its current candidate without running
the tool and records the method as skipped. Zero filled gaps after execution is
a valid reported result, not a claim of improvement. Real-tool acceptance is
reported separately from fixture-backed integration tests.

Accepted placement and handoff design:

- the complete recommendation places `gapcloser` after `hic_scaffolding` and
  before `t2t`;
- the implemented stage requires the current central `assembly`;
- successful adoption publishes an occurrence-owned assembly candidate and
  replaces the central `assembly` handoff consumed by T2T or a later Review.

A following Assembly Review binds to this GapCloser delivery. Base Polish
remains deferred.

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

The complete workflow recommendation includes this position with local
execution disabled; generic prefix expansion omits it.
