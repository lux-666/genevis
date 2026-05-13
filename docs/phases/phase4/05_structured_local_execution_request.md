# 05 Structured Local Execution Request

Task status: planned.

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

- [ ] Decide the exact file format: JSON first, YAML only if an existing parser
  path makes it low-risk.
- [ ] Add a run config schema under `app/schemas/` or another existing schema
  boundary.
- [ ] Add `--run-config` to `gene-agent run`.
- [ ] Validate the config before `Ledger.create_session(...)` and graph
  invocation.
- [ ] Merge `local_exec` with repeatable `--local-exec` flags deterministically.
- [ ] Seed `stage_outputs.assembly.flye_parameters` from the run config.
- [ ] Keep `gene-agent ask` on the existing structured run path; do not make the
  LLM control execution parameters beyond validated request fields.
- [ ] Add tests in `tests/test_entrypoints.py` and focused schema tests.
- [ ] Update Phase 4 task 04 smoke command to use the new run config surface.

## Acceptance

- A Phase 4 real smoke can be launched from CLI without a Python one-liner.
- Invalid run config input fails before graph invocation and before biological
  tools run.
- The default run path remains planning-first unless local execution is
  explicitly enabled.
- Existing tests remain green.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_entrypoints tests.test_assembly -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
