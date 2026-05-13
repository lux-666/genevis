# 05 Structured Local Execution Request

Task status: implemented.

## Goal

Replace the Phase 4 Python one-liner smoke path with a structured, validated
entry surface for local execution parameters.

The immediate need is to pass Flye parameters through a normal user-facing
contract instead of mutating `state["stage_outputs"]` inside an ad hoc script.
The design should also leave room for later stage parameters without turning the
CLI into a long list of biology-specific flags.

## Scope

Add a narrow run configuration contract for Phase 4 local execution.

Recommended shape:

```text
gene-agent run --manifest <manifest.yaml> --run-config <run-config.json>
```

The run config should be validated before graph invocation and should seed only
approved `stage_outputs` fields, such as:

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review"],
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 2000000,
      "threads": 4
    }
  }
}
```

## Boundaries

- Do not implement requested-stage routing.
- Do not introduce hidden defaults for Flye parameters.
- Do not let natural-language parsing mutate graph state directly.
- Do not add biological thresholds or automatic accept/reject policy.
- Do not duplicate method catalogs in the run config.
- Do not move descriptor, environment, or database provenance facts into request
  JSON.
- Do not implement durable resume in this task.

## Expected Outputs

- A Pydantic schema for the Phase 4 run config.
- CLI support for `--run-config`.
- Validation errors before graph invocation for:
  - unknown stages
  - unknown fields
  - missing Flye parameters when assembly local execution is requested
  - invalid `read_type`
  - non-positive `estimated_genome_size`
  - non-positive `threads`
- A merge function that seeds `stage_outputs` without overwriting unrelated
  state.
- Unit tests for valid config, invalid config, and interaction with existing
  `--local-exec` flags.
- Updated smoke instructions that no longer require the Python one-liner for
  routine Phase 4 acceptance.

## Tasks

- [x] Decide the exact file format: JSON first, YAML only if an existing parser
  path makes it low-risk.
- [x] Add a run config schema under `app/schemas/` or another existing schema
  boundary.
- [x] Add `--run-config` to `gene-agent run`.
- [x] Validate the config before `Ledger.create_session(...)` and graph
  invocation.
- [x] Merge `local_exec` with repeatable `--local-exec` flags deterministically.
- [x] Seed `stage_outputs.assembly.flye_parameters` from the run config.
- [x] Keep `gene-agent ask` on the existing structured run path; do not make the
  LLM control execution parameters beyond validated request fields.
- [x] Add tests in `tests/test_entrypoints.py` and focused schema tests.
- [x] Update Phase 4 task 04 smoke command to use the new run config surface.

## Acceptance

- A Phase 4 real smoke can be launched from CLI without a Python one-liner.
- Invalid run config input fails before graph invocation and before biological
  tools run.
- The default run path remains planning-first unless local execution is
  explicitly enabled.
- Existing tests remain green.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_entrypoints tests.test_run_config tests.test_assembly -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

## Implemented State

- `app/schemas/run_config.py` defines `FlyeRunParameters`, `AssemblyRunConfig`,
  and `RunConfig`. All three set `extra="forbid"` so unknown fields fail
  validation. `RunConfig.local_exec` is typed as the same six-stage
  `Literal` used by `--local-exec`, so an unknown stage is rejected the
  same way. `FlyeRunParameters.read_type` is restricted to
  `nano-raw | nano-hq | pacbio-raw`; `estimated_genome_size` and
  `threads` use `Field(gt=0)`. A `model_validator` rejects configs that
  put `"assembly"` in `local_exec` without `assembly.flye_parameters`.
- `app/schemas/run_config.load_run_config(path)` reads the JSON file,
  rejects non-object top-level payloads, and converts Pydantic
  `ValidationError` into a flat `ValueError` so the CLI layer prints a
  single error message.
- `gene-agent run --run-config PATH` loads and validates the file
  **before** `Ledger.create_session(...)` or `build_graph().invoke(...)`
  runs. Missing file, unparseable JSON, and schema violations all exit
  with code 1 and no ledger side-effects.
- The CLI merges `local_exec` deterministically: repeatable
  `--local-exec STAGE` flags first, then `run_config.local_exec`, with
  `dict.fromkeys(...)` preserving first-seen order. When the run config
  supplies `assembly.flye_parameters`, the CLI seeds
  `stage_outputs.assembly.flye_parameters` directly from
  `model_dump()`, which is the shape that
  `app/stages/assembly/nodes.py::_flye_parameters` already consumes.
- `gene-agent ask` is untouched. `EntryRequest.to_cli_argv()` never emits
  `--run-config`, so LLM-parsed intents cannot smuggle execution
  parameters into the graph. Local execution stays an explicit human
  choice on the structured `run` path.
- `docs/phases/phase4/04_real_biology_mainline_acceptance.md`
  "Suggested Manual Run Shape" now uses `gene-agent run --run-config`
  for the Phase 4 real-biology smoke.
- Tests:
  - `tests/test_run_config.py` covers empty config, unknown stage,
    unknown top-level and nested fields, `local_exec=["assembly"]`
    without `flye_parameters`, invalid `read_type`, non-positive
    `estimated_genome_size`, non-positive `threads`, valid Flye
    parameters without `assembly` in `local_exec`, JSON loader
    (missing file, bad JSON, non-object top-level, validation error).
  - `tests/test_entrypoints.py::RunConfigCLIIntegrationTests` covers
    seeding `stage_outputs` from a valid run config, `--local-exec` ∪
    `run_config.local_exec`, missing run-config file, invalid
    run-config, and parser shape.

## Example Run Config

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review"],
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 2000000,
      "threads": 4
    }
  }
}
```

Drive the Phase 4 real smoke with:

```bash
gene-agent run \
  --session-id phase4-real-mainline-smoke \
  --manifest <local manifest with raw_long_reads>.yaml \
  --data-dir data/phase4_real_smoke \
  --run-config run_config.json
```
