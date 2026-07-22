# Descriptor Spec

Descriptors declare executable method capability and keep package compatibility
out of graph-routing code.

## Registry

`descriptors/registry.yaml` maps package names and canonical method IDs to their
descriptor files and defines the ordered methods available in each stage:

```yaml
packages:
  flye: flye.yaml
methods:
  assembly.candidate.flye: assembly/flye_candidate.yaml
by_stage:
  assembly:
    - assembly.candidate.flye
```

Every method in `by_stage` is executable. Future or unavailable tools remain
outside the registry until their adapter, routing, configuration, and tests are
implemented. Registry loading goes through `app.configuration` so packaged and
source-checkout execution are independent of the current working directory.

## Package Descriptor

A package descriptor defines tool-level capability:

```yaml
package_name: flye
version_constraint: ">=2.9"
language: cli
install_channel: conda
entry_patterns:
  - "flye --nano-raw {reads} --out-dir {output_dir} --threads {threads}"
resource_defaults:
  cpu_cores: 32
  memory_gb: 128
  walltime_hours: 48
```

Package descriptors may describe installation channels, entry patterns,
resource defaults, and known pitfalls. Machine-specific environment and package
roots live in `config/envs.yaml`, not in descriptor capability records.

## Method Descriptor

A method descriptor binds one package capability to a stage:

```yaml
method_name: assembly.candidate.flye
stage: assembly
package_ref: flye
parameter_catalog: flye
required_inputs:
  - name: long_reads
    type: clean_long_reads_or_fofn
expected_outputs:
  - name: draft_assembly
    type: draft_assembly
review_rules:
  - candidate_bundle_exists
planner:
  required_parameters: [threads]
  parameter_defaults:
    threads: 4
```

Method IDs are canonical across registry, planner, run config, stage routing,
execution evidence, and artifacts. An explicit canonical method selection has
priority; runtime still validates that its stage, inputs, read type, and
parameters match the descriptor and run configuration.

Descriptors do not authorize process execution. Execution requires the stage to
be enabled for local execution and a complete frozen attempt.

## Asset Rules

Asset descriptors define which manifest asset types may satisfy a stage input.
They are inputs to inventory and resource resolution, not a second execution or
cross-stage handoff system. Current logical resource transport is defined in
[`architecture/resource-handoff.md`](architecture/resource-handoff.md).
