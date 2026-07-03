# Task 7: Refactor Command Builder Around L0/L1/L2

**Phase**: 10
**Status**: Pending
**Depends on**: Tasks 1-3 and 6
**Created**: 2026-07-02
**Updated**: 2026-07-03

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
) -> dict[str, dict[str, Any]]:
    ...
```

Responsibilities:

- derive L0 from execution context: prefix, threads, input reads, attempt-specific work dirs, fixed route flags;
- derive L1 from read_qc, genome_survey, manifest, and run_config;
- insert only validated L2 recommendations;
- return the exact object stored in the attempt ledger.

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

## Acceptance Criteria

- [ ] Command builder implemented.
- [ ] hifiasm_ont freeze uses command builder.
- [ ] Attempt metadata records `parameter_sources` and `argv`.
- [ ] Retry attempts do not overwrite prior attempt directories.
