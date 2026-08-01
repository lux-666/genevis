# TUI Midstage Entry Design

## Goal

Allow the conversational TUI to start an immutable run from either of two
externally supplied, already prepared resources:

1. `clean_long_reads` starts at `assembly`.
2. `assembly_fasta_registered` starts at `assembly_review`.

The feature must use the existing manifest, inventory, resource-resolution,
freeze, and review paths. It must not add a second execution mode or permit
unchecked paths to bypass the frozen-input boundary.

## Scope

This slice supports exactly these entry modes:

| Entry mode | Required TUI fact | Manifest asset | Earliest selectable stage |
| --- | --- | --- | --- |
| `raw_reads` | existing HiFi / ONT facts | `raw_long_reads` | existing behavior |
| `clean_long_reads` | cleaned long-read paths and explicit `ont` or `pacbio_hifi` platform | `clean_long_reads` | `assembly` |
| `existing_assembly` | assembly FASTA path | `assembly_fasta_registered` | `assembly_review` |

No generic arbitrary-stage importer is included. Other stages retain their
current resource requirements and will continue to block when required inputs
are absent.

## Intake Contract

`WorkflowIntent` gains a durable entry-mode field. The deterministic intent
parser recognizes explicit wording such as "clean reads", "skip read QC",
"start from assembly", "existing contig", and "start from assembly review".
The optional LLM intent result may provide this same typed field, with the raw
user request remaining authoritative for the goal text.

The mutable TUI draft gains typed facts for the two new resources. A cleaned
read fact includes the declared long-read platform; the system does not infer
ONT versus HiFi from a filename. An existing-assembly fact contains one or
more readable FASTA paths. Both facts participate in the existing append-only
answer history, unresolved-candidate reporting, replacement confirmation, and
revision-checked persistence.

The intake requirement queue derives from the entry mode:

- `raw_reads` retains the current HiFi/ONT/Hi-C logic.
- `clean_long_reads` asks only for cleaned reads and their explicit platform;
  it does not request raw reads or perform the Hi-C/T2T batch dialogue.
- `existing_assembly` asks only for assembly FASTA input; it does not request
  reads or Hi-C.

The deterministic asset parser accepts explicit labels for cleaned ONT reads,
cleaned HiFi reads, and existing assembly. Unlabelled paths are accepted only
for the currently focused intake role. All inputs use the current bounded
checks: exact user-supplied path, regular/readable/non-empty file, and the
appropriate supported FASTQ/FASTA suffix. No directory expansion or filename
role inference is permitted.

## Manifest and Planning

`materialize_manifest_draft()` projects the new facts into the existing asset
types with an `intake_role` provenance marker and the supplied platform where
applicable. The existing `InputManifest` and inventory stage remain the only
places where those inputs become logical `long_reads` or `assembly` resources.

The TUI planner state includes the entry mode in its intake-goal context. The
plan-generation prompt tells the planner that clean reads may begin at
`assembly` and existing assemblies may begin at `assembly_review`; it must not
reinsert `read_qc` or `genome_survey` merely because those stages precede
assembly in the canonical order. Operators may still edit the selected stages
in the existing Plan Draft UI.

Planner helpers that currently inspect only `raw_long_reads` become
long-read-aware and inspect both raw and clean assets. This keeps automatic
platform/read-type defaults, assembly-method suggestions, parameter requests,
and recommended-stage construction consistent with the manifest. Since no
genome-survey stage precedes a clean-read `assembly` entry, assembly still asks
for a supplied genome-size estimate when the selected assembler requires one.

For an existing assembly, the existing inventory behavior remains authoritative:
it registers the FASTA as the central `assembly` resource, records the three
upstream stages as `existing_assembly_input` skips, and routes to
`assembly_review`.

## Failures and Invariants

- A clean-read submission without an explicit ONT or HiFi declaration remains
  incomplete; it cannot select an assembler from a guessed filename.
- Invalid, absent, empty, or unsupported files remain unresolved and never
  enter the manifest.
- The planner must not produce a `clean_long_reads` assembly plan with no
  platform/read type or an `existing_assembly` review plan with no registered
  FASTA.
- Freeze, immutable dependency capture, execution locks, review gates, reruns,
  and inventory provenance retain their current behavior.
- Existing raw-read TUI sessions and serialized draft fields remain valid.

## Tests and Documentation

Focused tests will cover deterministic intent detection, requirement queues,
platform-explicit cleaned-read parsing and rejection of ambiguous input,
existing-assembly parsing, mutable manifest projection, TUI planner defaults,
and frozen execution inputs. Existing inventory tests already verify the
existing-assembly skip route; an intake-to-planner test will bind that route to
the TUI contract.

The README TUI guide will describe both entry phrases and the assets they
register. It will remove the statement that existing assemblies are outside
the TUI intake scope.
