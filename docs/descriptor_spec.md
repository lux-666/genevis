# Descriptor Spec

Descriptors keep biological method compatibility out of graph routing code.

## Registry

`descriptors/registry.yaml` maps package and method names to descriptor files and groups methods by stage.

```yaml
packages:
  flye: flye.yaml
methods:
  flye_ont_raw: flye_ont_raw.yaml
by_stage:
  assembly:
    - flye_ont_raw
```

## Package Descriptor

Package descriptors describe tool-level capability:

```yaml
package_name: flye
version_constraint: ">=2.9"
language: cli
install_channel: conda
entry_patterns:
  - "flye --nano-raw {reads} --out-dir {out_dir}"
help_summary: "Repeat-graph assembler for raw single-molecule reads."
known_pitfalls:
  - "Memory scales with genome size and coverage."
resource_defaults:
  cpu_cores: 64
  memory_gb: 256
  walltime_hours: 72
```

## Method Descriptor

Method descriptors describe stage-specific use:

```yaml
method_name: flye_ont_raw
stage: assembly
package_ref: flye
goal: "Assemble ONT raw reads into draft contigs."
required_inputs:
  - name: clean_long_reads
    type: clean_long_reads
    description: "Registered long-read FASTQ input."
expected_outputs:
  - name: draft_assembly
    type: draft_assembly
    description: "Draft assembly FASTA."
review_rules:
  - assembly_fasta_exists
human_review_triggers:
  - memory_fallback_required
```

## Asset Rules

`descriptors/assets/ont_clr_long_reads.yaml` defines asset-stage compatibility. These rules are planner inputs, not execution code.
