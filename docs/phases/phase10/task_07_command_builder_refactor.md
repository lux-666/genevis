# Task 7: Refactor Command Builder Around L0/L1/L2

**Phase**: 10
**Status**: Completed
**Depends on**: Tasks 1-3 and 6
**Created**: 2026-07-02
**Updated**: 2026-07-07

## Goal

Replace hardcoded hifiasm/hifiasm_ont command construction with a catalog-aware builder that merges:

- `l0_fixed`: executor and route mechanics,
- `l1_readonly`: derived factual values,
- `l2_recommended`: validated LLM recommendations.

The command builder must be deterministic and replayable. It must never accept raw LLM output directly; it consumes only validated L2 parameters.

## Current Problem

`freeze_hifiasm_ont_attempt()` currently writes a command directly:

```python
hifiasm -o {prefix} -t {threads} {read_flag} {reads}
```

This cannot record parameter provenance or replay the exact L0/L1/L2 merge.

## Target API

```python
def build_command(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    parameter_sources: dict[str, dict[str, Any]],
) -> list[str]:
    ...
```

Input:

```python
parameter_sources = {
    "l0_fixed": {
        "output_prefix": "work/hifiasm_ont/out/asm",
        "threads": 32,
        "input_reads": ["reads.fq"],
        "ont_mode": True,
    },
    "l1_readonly": {
        "hg_size": "1g",
        "hom_cov": 36,
    },
    "l2_recommended": {
        "purge_level": 1,
        "read_length_cutoff": 3000,
        "score_cutoff": 10,
    },
}
```

Output:

```python
[
    "hifiasm",
    "--ont",
    "-o", "work/hifiasm_ont/out/asm",
    "-t", "32",
    "--hg-size", "1g",
    "--hom-cov", "36",
    "-l", "1",
    "--rl-cut", "3000",
    "--sc-cut", "10",
    "reads.fq",
]
```

## Merge Rules

- `l0_fixed` may contain only catalog level 0 keys.
- `l1_readonly` may contain only catalog level 1 keys.
- `l2_recommended` may contain only catalog level 2 keys.
- Unknown keys are fatal.
- Wrong-level keys are fatal.
- Boolean flags emit only when truthy.
- `null`, empty string, and `"default"` values are omitted unless the catalog says otherwise.
- Positional inputs (`cli: null`) are appended at the end.
- Parameter order is deterministic:
  1. fixed mode booleans such as `--ont`,
  2. other L0 flags,
  3. L1 facts,
  4. L2 recommendations,
  5. positional inputs.

## Source Builder

Add a helper:

```python
def build_parameter_sources(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    state: TopLevelState,
    assembly_out: dict[str, Any],
    attempt_id: int,
    recommended_params: dict[str, Any],
    execution_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    ...
```

Responsibilities:

- derive L0 from catalog `fixed_value` and `source_priority` paths such as `execution.output_prefix`, `assembly.hifiasm_ont_parameters.threads`, and `execution.input_reads`;
- derive L1 from catalog `source_priority` paths into read_qc, genome_survey, manifest, shared parameters, or run_config;
- insert only validated L2 recommendations;
- return the exact object stored in the attempt ledger.

Do not hard-code special parameter names such as `output_prefix`, `threads`, or `input_reads` inside the generic builder. Method adapters may pass an `execution_context`, but the catalog decides which parameters read from it.

## hifiasm_ont Freeze Integration

`freeze_hifiasm_ont_attempt()` should:

1. read `current_parameter_attempt_id`,
2. create attempt-specific working/output/artifact directories,
3. build `parameter_sources`,
4. build `argv`,
5. write the shell script from escaped `argv`,
6. store both `parameter_sources` and `argv` in `FrozenAttemptSpec.metadata`,
7. append/update the current `parameter_attempts` entry with `status: frozen`.

Attempt paths must include attempt id, for example:

```text
data/work/assembly/<session>/hifiasm_ont/attempt_0/run_hifiasm_ont.sh
data/artifacts/assembly/<session>/hifiasm_ont/attempt_0/draft_assembly.fasta
```

This prevents retry attempts from overwriting prior outputs.

## Replay Requirements

The attempt record must include:

- catalog path,
- parameter sources,
- argv,
- script path,
- frozen attempt metadata,
- expected outputs.

Replaying an attempt should not require the LLM.

## Tests

- Build hifiasm_ont command with L0 `--ont`, L1 `--hg-size`, and L2 `--rl-cut`.
- Reject unknown parameter.
- Reject L1 key in `l2_recommended`.
- Reject L2 key in `l1_readonly`.
- Omit `"default"` values.
- Append input reads last.
- Ensure attempt 0 and attempt 1 produce different directories.
- Derive L0 fields through catalog `source_priority`, not hard-coded parameter names.
- Missing catalog or malformed recommended-parameter state returns structured failure from method freeze nodes.

## Acceptance Criteria

- [x] Command builder implemented.
- [x] hifiasm_ont freeze uses command builder.
- [x] Attempt metadata records `parameter_sources` and `argv`.
- [x] Retry attempts do not overwrite prior attempt directories.
- [x] Generic source derivation is catalog-driven.
- [x] Catalog-loading and malformed-state errors fail gracefully.

## Implementation Notes

**Implemented slice**: 2026-07-03

### Key Implementation Points

1. **Command builder**: `app/stages/assembly/command_builder.py`
   - `build_command()` merges L0/L1/L2 source groups into deterministic argv
   - `build_parameter_sources()` derives L0/L1 from state via catalog source_priority
   - Validates parameter levels match their source groups
   - Emits boolean flags only when truthy, omits "default" values

2. **Integration**: Both hifiasm.py and hifiasm_ont.py freeze functions now:
   - Load parameter catalog via `load_parameter_catalog()`
   - Build parameter sources with execution_context for L0 derivation
   - Call `build_command()` to generate argv
   - Store `parameter_sources` and `argv` in attempt metadata for replay

3. **Error handling**: Catalog loading and parameter command errors return structured failures via `fail_update()`

### Completion Notes

- hifiasm and hifiasm_ont freeze paths now use
  `parameter_attempt_<attempt_id>/` scoped work and artifact directories.
- Freeze functions update the current typed `parameter_attempts` entry with
  `parameter_sources`, `argv`, script paths, frozen metadata, and expected
  outputs.
- `input_profile_ref` and `prompt_payload_ref` are written by the recommender
  node and stored on the same typed attempt entry.
