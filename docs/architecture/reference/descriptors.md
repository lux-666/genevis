# Descriptor Contract

> **Status:** implemented, with the inert-field gap recorded in
> [Architecture conformance](conformance.md).  
> **Read when:** package capability, method identity, required inputs, expected
> outputs, or planner metadata must be checked.  
> **Comes from:** planning, bootstrap, and stage method selection.  
> **Feeds into:** [Execution](../topics/execution.md) and
> [Parameter recommendation](../topics/parameter-recommendation.md).  
> **Related contracts:** [Stage contracts](stage-contracts.md),
> [Governance](governance.md).  
> **Code evidence:** `descriptors/registry.yaml`,
> `app/schemas/descriptors.py`, `app/configuration.py`,
> `app/entrypoints/planner_catalog.py`, and `tests/test_configuration.py`.

Descriptors declare registered method/planner capability and keep package
compatibility out of graph-routing code. A local capability is executable; an
explicit `planner_only` capability has tested no-process semantics.

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

Every method in `by_stage` is an implemented, selectable capability. Most use
`planner.execution_kind: local`; `read_qc.length_filter.skip` is deliberately
`planner_only` and represents a real no-process selection rather than an
executable command. Future or unavailable tools remain outside the registry
until their adapter or planner-only behavior, routing, configuration, and tests
are implemented. Registry loading goes through `app.configuration` so packaged
and source-checkout execution are independent of the current working directory.

## Package Descriptor

A package descriptor defines tool-level capability:

```yaml
package_name: flye
version_constraint: ">=2.9"
language: cli
install_channel: conda
entry_patterns:
  - "flye --nano-raw {reads} --out-dir {output_dir} --threads {threads}"
help_summary: "Long-read de novo assembler for noisy long reads."
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
goal: "Build one Flye draft-assembly candidate."
required_inputs:
  - name: long_reads
    type: clean_long_reads_or_fofn
    description: "Clean long reads or an approved long-read FOFN."
expected_outputs:
  - name: draft_assembly
    type: draft_assembly
    description: "Draft assembly FASTA produced by the selected attempt."
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

## Current validation boundary

The registry loader currently proves that every referenced package exists and
that every `by_stage` method exists and declares the same stage. The committed
registry also has matching registry keys and descriptor names, and every method
record appears in exactly one stage list. Those last identity/completeness facts
are true of the current files, but the loader does not enforce them explicitly.

`PackageDescriptor`, `MethodDescriptor`, and `IOSpec` currently use Pydantic's
default handling for undeclared fields: unknown YAML keys are ignored rather
than rejected or retained. Consequently, a field not declared in
`app/schemas/descriptors.py` is not runtime capability metadata merely because
it appears in YAML. The current Flye input contains two such inert keys;
[Architecture conformance](conformance.md) records the required cleanup or
schema decision.
