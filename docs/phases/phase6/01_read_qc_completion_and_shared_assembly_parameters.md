# 01 Read QC Completion And Shared Assembly Parameters

Task status: completed.

## Goal

Finish `read_qc` as the upstream read-characterisation and handoff stage for
the long-read local assembly mainline. Read QC does not estimate genome size in
this task. Instead, the ask/planning layer collects shared assembly facts such
as `read_type` and `estimated_genome_size` once, then Flye, NextDenovo, Canu,
hifiasm, and future methods reuse those facts through method-specific
resolvers.

The intended mainline becomes:

```text
natural-language request
-> ask/planning: collect shared assembly parameters when needed
-> raw long reads
-> read_qc stats / explicit length-filter decision / FOFN handoff
-> assembly candidate planning / optional local execution
```

Flye still needs a concrete `--genome-size` value before execution. NextDenovo
and Canu also need a genome-size prior. All assembly methods also care about
the read technology/type, even when each tool uses different parameter names.
The workflow should ask once and store normalized shared facts, not ask again
under every method.

## Current Problem

Phase 4 made local Flye execution require:

```json
{
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 2000000,
      "threads": 4
    }
  }
}
```

That was useful for a first real smoke, but it is now too operator-heavy. Once
assembly planning presents multiple candidate methods, the same genome-size
value may be needed by more than one method. If the planner asks for
`estimated_genome_size` under Flye and then asks again under NextDenovo, the UI
is duplicating a biological fact that should be shared.

The same problem exists for read type. Flye currently wants values like
`nano-raw`; NextDenovo, Canu, and hifiasm use different names or config fields,
but the user's biological answer is the same. The normal ask flow should collect
a normalized `read_type` once and let assembly method resolvers translate it.

For this task, do not require read QC to estimate genome size. That can be a
later task. Treat genome size as an operator-provided shared parameter or an
existing manifest prior, then thread that value through all methods that need
it.

## Scope

- Complete the read QC output contract so downstream assembly can consume:
  - read statistics summary;
  - explicit length-filter decision;
  - optional approved filtered-read refs;
  - stable FOFN handoff refs;
  - compact QC artifact refs.
- Add a shared parameter contract for `estimated_genome_size` so it is collected
  once and reused by Flye, NextDenovo, Canu, and future genome-size-dependent
  methods.
- Add normalized shared parameters for `read_type` and
  `estimated_genome_size`.
- Map normalized `read_type` values into method-specific command/config values
  during assembly parameter resolution.
- Make `assembly.flye_parameters.read_type` and
  `assembly.flye_parameters.estimated_genome_size` optional when shared
  parameters or manifest priors already provide valid values.
- Teach assembly method parameter resolution to use this priority order:
  1. method-specific explicit override;
  2. shared workflow parameter, such as
     `shared_parameters.estimated_genome_size` or
     `shared_parameters.read_type`;
  3. inventory/manifest prior;
  4. future upstream estimates, when implemented;
  5. block before command execution with a clear missing-parameter reason.
  Method-specific genome-size or read-type override is supported only through
  explicit config or advanced mode. The normal ask flow should prompt only once
  for the shared `estimated_genome_size` and shared `read_type`.
- Keep `read_type` and `threads` explicit or infer only through already
  documented safe rules.
- Present read QC length filtering as an explicit operator choice:

```text
[read_qc] length filtering:
  1. skip filtering, use raw reads
  2. seqkit_seq
  3. chopper
  4. filtlong
  5. fastplong
```

Default is `skip filtering, use raw reads`.

## Non-Goals

- Do not make read QC estimate genome size in this task.
- Do not invent genome size from total read bases alone.
- Do not add automatic biological thresholds for filtering, assembly acceptance,
  purge, polish, or review decisions.
- Do not run multiple length filters in one read QC pass.
- Do not make filtering mandatory before Flye.
- Do not bypass the existing `assembly_review` human gate for generated
  assemblies.
- Do not store large QC reports, FASTQ/FASTA contents, command logs, or metric
  tables in `TopLevelState`.

## Proposed Contract

Read QC should publish the selected filtering route and FOFN handoff as compact
state plus artifacts:

```json
{
  "stage_outputs": {
    "read_qc": {
      "length_filter_method": "skip",
      "length_filter_decision": "skip filtering, use raw reads",
      "summary_artifact_ref": "data/artifacts/read_qc/<session>/read_qc_summary.json",
      "assembly_input_ref": "data/artifacts/read_qc/<session>/long_reads.fofn"
    }
  }
}
```

The filter method values should be:

- `skip`
- `read_qc.length_filter.seqkit_seq`
- `read_qc.length_filter.chopper`
- `read_qc.length_filter.filtlong`
- `read_qc.length_filter.fastplong`

Only the selected filter route may run. `skip` means write `long_reads.fofn`
from raw reads and continue.

The shared assembly parameter surface should hold cross-method facts:

```json
{
  "shared_parameters": {
    "read_type": "ont_raw",
    "estimated_genome_size": 2000000
  }
}
```

Use this exact shared-parameter path across planning and runtime:

```text
ask_plan.shared_parameters
-> run_config.shared_parameters
-> runtime state shared_parameters
```

Do not place user-provided shared parameters under
`stage_outputs.assembly.shared_parameters`. `stage_outputs` is for stage
outputs; these values are request/planning inputs known before assembly runs.

Allowed normalized `read_type` values:

- `ont_raw`
- `ont_hq`
- `pacbio_clr`
- `pacbio_hifi`

Assembly method resolvers translate normalized values into tool-specific
parameters. Initial mapping:

| Shared `read_type` | Flye | NextDenovo | Canu | hifiasm |
| --- | --- | --- | --- | --- |
| `ont_raw` | `--nano-raw` | `read_type = ont` | ONT/Nanopore mode | not preferred |
| `ont_hq` | `--nano-hq` | `read_type = ont` | ONT/Nanopore mode | not preferred |
| `pacbio_clr` | `--pacbio-raw` | `read_type = clr` | PacBio raw mode | not preferred |
| `pacbio_hifi` | `--pacbio-hifi` | `read_type = hifi` | PacBio HiFi mode | HiFi mode |

## Run-config Change

Allow local assembly execution without method-specific
`assembly.flye_parameters.read_type` or
`assembly.flye_parameters.estimated_genome_size` when shared parameters or
manifest priors can provide them:

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review"],
  "shared_parameters": {
    "read_type": "ont_raw",
    "estimated_genome_size": 2000000
  },
  "assembly": {
    "flye_parameters": {
      "threads": 4
    }
  }
}
```

Validation should still reject local Flye execution if neither run-config,
ask-plan/runtime state, nor manifest can supply a valid shared `read_type` and
positive integer genome size before the Flye command is frozen.

## Implementation Notes

- `app/stages/read_qc/nodes.py` is the primary place to extend read QC filtering
  decisions and committed output fields.
- `app/schemas/run_config.py` should relax only the run-config requirement for
  `FlyeRunParameters.estimated_genome_size`; it must still validate the value
  when provided.
- `app/schemas/ask_plan.py`, `app/schemas/run_config.py`, and
  `app/schemas/state.py` should expose `shared_parameters` with
  `read_type` and `estimated_genome_size` fields.
- `app/stages/assembly/nodes.py::_genome_size_prior` should read the shared
  parameter before falling back to inventory/manifest values and future
  estimates.
- `app/stages/assembly/nodes.py` should add a normalized read-type resolver
  that maps shared `read_type` into each method's command/config vocabulary.
- `app/stages/assembly/nodes.py::_flye_parameters` should merge shared
  `read_type` and upstream `genome_size_prior` into Flye parameters before
  deciding that parameters are incomplete.
- `app/entrypoints/ask_planner.py` should stop prompting for Flye
  `read_type` or `estimated_genome_size` as Flye-specific fields. Ask for one
  shared `read_type` and one shared `estimated_genome_size` only when selected
  assembly methods require them and no manifest/shared value is already present.

## Tasks

- [x] Add the read QC length-filter selection contract with options:
  skip, seqkit_seq, chopper, filtlong, fastplong.
- [x] Ensure skip filtering writes an explicit decision record and raw-read
  `long_reads.fofn`.
- [x] Ensure exactly one selected filtering route can run, and only with
  explicit user thresholds/config.
- [x] Add a shared `read_type` and `estimated_genome_size` parameter surface to
  ask-plan, run-config, and runtime state.
- [x] Add normalized read-type validation with allowed values:
  `ont_raw`, `ont_hq`, `pacbio_clr`, `pacbio_hifi`.
- [x] Update assembly genome-size resolution to prefer method override, shared
  parameter, then manifest/inventory prior.
- [x] Add assembly read-type resolution that maps shared `read_type` to
  method-specific Flye, NextDenovo, Canu, and hifiasm values.
- [x] Relax run-config validation so Flye-specific `estimated_genome_size` is
  optional when the shared parameter or manifest prior exists.
- [x] Keep Flye freezing blocked when no positive genome size is available from
  any approved source.
- [x] Update `gene-agent ask` parameter prompts so genome size and read type
  are asked once as shared assembly facts, not once per method.
- [x] Add tests for:
  - read QC filtering option rendering and skip behavior;
  - default read QC filtering choice is skip;
  - one selected filter method only;
  - shared read-type validation and method-specific mapping;
  - Flye local execution using shared genome size without Flye-specific
    `estimated_genome_size`;
  - NextDenovo/Canu/hifiasm planning reusing shared genome size and read type
    without a second prompt;
  - explicit advanced method-specific override taking precedence over the
    shared value;
  - missing shared/manifest genome size or read type still blocking Flye before
    command execution;
  - no large QC payloads leaking into `TopLevelState`.

## Acceptance

- The read QC prompt presents exactly:
  `skip filtering, use raw reads`, `seqkit_seq`, `chopper`, `filtlong`,
  `fastplong`.
- The default read QC filter choice is `skip filtering, use raw reads`.
- A local `read_qc -> assembly.candidate.flye` run can execute Flye without
  method-specific `read_type` or
  `assembly.flye_parameters.estimated_genome_size`, provided the user supplied
  shared `read_type` and positive `estimated_genome_size` or the manifest
  already contains compatible priors.
- The generated Flye script still contains a concrete `--genome-size <value>`.
- The read type used for Flye is mapped from normalized shared `read_type`.
- The values used for Flye are auditable as shared parameters and are reused by
  other assembly methods.
- If no valid estimate exists, the workflow blocks with a clear
  `estimated_genome_size` missing reason before Flye runs.
- Existing explicit genome-size inputs continue to work and take precedence.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_assembly tests.test_run_config tests.test_ask_plan -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
