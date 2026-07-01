# 03 Flye, QUAST, And Human Gate Smoke

Task status: implemented.

## Goal

Run the Phase 4 mainline closure:

```text
FOFN / long reads
-> Flye
-> assembly.fasta
-> QUAST
-> quast_summary.json
-> blocked_human
```

This task proves that `assembly` can produce a real draft assembly and
`assembly_review` can produce the first real review report, while still
requiring a human decision before promotion.

## Scope

Use only the primary Flye route for ONT/CLR draft assembly and the existing
QUAST route for reference-free assembly review.

Flye must require explicit inputs and parameters:

- long-read input or FOFN
- read type
- estimated genome size
- threads

Missing required Flye parameters should produce `blocked_param`, not an
invented default and not an attempted run.

QUAST should run only after a materialized assembly FASTA exists and the QUAST
environment/tool preflight succeeds.

## Boundaries

- Do not run NextDenovo or Canu as automatic fallback assemblers.
- Do not use hifiasm for this ONT/CLR mainline.
- Do not run BUSCO, Merqury, mapping coverage, purge_dups, or contamination
  checks in this task.
- Do not polish, purge, scaffold, repeat-annotate, gene-annotate, or
  function-annotate in this task.
- Do not auto-accept or auto-reject assemblies from QUAST metrics.
- Do not invent QUAST thresholds.
- Do not continue past `blocked_human` after generated-candidate QUAST review.

## Expected Outputs

Assembly:

- Flye command stdout/stderr
- Flye log if produced
- materialized `assembly.fasta` or existing artifact equivalent
- executed Flye candidate bundle
- ledger attempt lifecycle events

Assembly review:

- QUAST command stdout/stderr
- QUAST `report.tsv`
- optional QUAST `report.txt`
- `quast_summary.json`
- updated assembly evaluation bundle
- `assembly_review_request.json`
- final state `blocked_human`

Status vocabulary should distinguish:

- `completed` for successful method-level artifacts before review blocking
- `blocked_param`
- `blocked_input`
- `blocked_env`
- `missing_tool`
- `failed_retryable`
- `failed_fatal`
- `blocked_human`

## Tasks

- [x] Add Flye parameter validation before local Flye execution.
- [x] Require explicit `read_type`, `estimated_genome_size`, and `threads` for
  Phase 4 local Flye smoke.
- [x] Route missing Flye parameter cases to `blocked_param` with a clear missing
  parameter list.
- [x] Ensure Flye runs through the configured shared `env_geno_exec`
  environment (single-env policy; see `config/envs.yaml`).
- [x] Register the materialized Flye assembly FASTA and command artifacts.
- [x] Ensure Flye failure does not trigger NextDenovo, Canu, or hifiasm.
- [x] Ensure QUAST runs through the same shared `env_geno_exec` environment.
- [x] Parse QUAST report into `quast_summary.json` without applying thresholds.
- [x] Write or update the review request artifact with candidate id, Flye
  provenance, QUAST artifact refs, and non-decisional metrics.
- [x] Ensure generated assemblies stop at `blocked_human` after QUAST.
- [x] Add tests for missing Flye params, missing Flye env/tool, Flye success,
  QUAST success, QUAST missing env/tool, and no auto-promotion.
- [x] Add a documented small-real-data smoke command.

## Acceptance

- A small real long-read smoke can produce a real Flye assembly when the Flye
  environment and required parameters exist.
- QUAST evaluates the materialized assembly when the QUAST environment exists.
- QUAST output is registered and summarized.
- The workflow stops at `blocked_human`.
- No final assembly is promoted without a human review decision.
- Non-mainline review methods remain candidate-only or blocked-reason records.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_assembly_review tests.test_review_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Suggested real smoke: drive the graph from a short Python one-liner that
seeds the two things the CLI cannot carry today (`execution_mode = "local"`
for assembly + assembly_review, and the Flye parameters dict). The one-liner
re-uses the committed `tests/fixtures/phase4/read_qc_manifest.yaml` for the
inventory + read QC side; point it at a real CycloneSEQ / PacBio FASTQ for a
non-fixture run.

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python - <<'PY'
from app.graph.builder import build_graph
from app.schemas.state import initial_state

state = initial_state(
    session_id="phase4-flye-quast-smoke",
    input_manifest_ref="tests/fixtures/phase4/read_qc_manifest.yaml",
    data_dir="data/phase4_smoke",
)
state["stage_outputs"] = {
    "read_qc": {"execution_mode": "local"},
    "assembly": {
        "execution_mode": "local",
        "flye_parameters": {
            "read_type": "nano-raw",
            "estimated_genome_size": 2_000_000,
            "threads": 4,
        },
    },
    "assembly_review": {"execution_mode": "local"},
}

graph = build_graph()
result = graph.invoke(state, config={"configurable": {"thread_id": state["session_id"]}})
print("current_stage:", result.get("current_stage"))
print("supervision_state:", result.get("supervision_state"))
PY
```

## Human Decisions Before Coding

- Exact source of `estimated_genome_size` for Phase 4: manifest hint,
  `stage_outputs.assembly.flye_parameters`, or a separate structured request.
- Accepted Flye `read_type` values for this phase, such as `nano-raw`,
  `nano-hq`, or `pacbio-raw`.
- Whether missing Flye/QUAST env should block the stage or fail terminally in
  local execution mode.
- Whether `quast_summary.json` should be a new artifact type or stored as
  metadata inside the existing evaluation bundle.

Resolutions (captured during implementation):

- `estimated_genome_size` is read directly from
  `stage_outputs.assembly.flye_parameters.estimated_genome_size`. No fallback
  chain into genome_survey or inventory is used for Phase 4 Flye runs.
- Accepted `read_type` values are `nano-raw`, `nano-hq`, and `pacbio-raw`,
  mapping to Flye CLI flags `--nano-raw`, `--nano-hq`, `--pacbio-raw`.
- Missing/invalid Flye env or tool produces `blocked_env` / `missing_tool`
  via the shared `EnvResolver` preflight (task 01); missing required Flye
  parameters produce `blocked_param`. Each routes to the stage's terminal
  failure edge without attempting the biological command.
- `quast_summary.json` is its own artifact type
  `assembly_review.quast_summary`, registered as a first-class `ArtifactMeta`
  and referenced from the QUAST signal metadata (`quast_summary_ref`).

## Implemented State

- Strict Flye parameter validation lives in
  `app/stages/assembly/nodes.py::_flye_parameters`. It requires
  `flye_parameters.read_type`, `flye_parameters.estimated_genome_size`, and
  `flye_parameters.threads`. Missing / non-positive / unknown values produce
  `status: "blocked_param"` with `missing_params: list[str]` and
  `invalid_params: list[dict]` (param, value, accepted/reason). No Flye run,
  no ledger attempt event, no fallback assembler.
- `FLYE_READ_TYPE_FLAGS` maps `nano-raw|nano-hq|pacbio-raw` to their Flye CLI
  flags. The generated `run_flye.sh` now passes the validated `read_type`
  flag plus `--genome-size <bp>` plus `--threads N`. The frozen attempt
  metadata records `read_profile`, `estimated_genome_size`, and `threads`
  so the execution provenance is self-describing.
- `app/stages/assembly/routing.py` adds `blocked_param` to `FAIL_STATUSES`
  alongside `blocked_env` / `missing_tool` (task 01). `fail_assembly` maps
  each to a human-readable `last_error`.
- `app/schemas/artifacts.py` adds `ASSEMBLY_REVIEW_QUAST_SUMMARY =
  "assembly_review.quast_summary"`.
- `app/stages/assembly_review/nodes.py::collect_quast_artifacts` now writes
  `quast_summary.json` next to the QUAST `report.tsv` artifact directory
  on every successful QUAST attempt. The payload carries `session_id`,
  `method_name`, `candidate_id`, `attempt_id`, `draft_assembly_ref`,
  `report_tsv_ref`, parsed `metrics` (contig_count, largest_contig,
  total_length, n50, l50, gc_percent when present), and
  `notes: ["thresholds_not_applied_without_human_approval"]`. It is
  registered as an `ArtifactMeta`, appended to the artifacts index and
  ledger events, threaded into the QUAST signal metadata as
  `quast_summary_ref`, and added to the signal's `metric_refs` so the
  human review request payload exposes it alongside the raw TSV/TXT.
- Tests covering the new vocabulary:
  - `tests/test_assembly.py::Phase4FlyeParameterTests` — missing params →
    `blocked_param`, unknown `read_type` → `invalid_params`, full params
    drive the expected `--nano-hq --genome-size 2000000 --threads 1`
    invocation and still produce a `draft_ready` handoff with the
    executed candidate bundle.
  - `tests/test_assembly_review.py::test_local_quast_success_writes_quast_summary_artifact`
    — asserts `assembly_review.quast_summary` is registered in
    `artifacts.jsonl`, reachable via `quast_summary_ref`, carries the
    parsed metrics, and appears on the signal's `metric_refs`.
- `blocked_env` / `missing_tool` paths for both Flye and QUAST are
  already covered by the shared `EnvResolver` + `preflight` suite
  (`tests/test_env_resolver.py`) and the stage-level dispatcher
  wiring introduced in task 01; no redundant per-stage copies were added.

## Suggested Local Smoke

Real-biology smoke remains human-driven: Flye needs real long-read coverage,
so no committed fixture can stand in. Use the same Python-one-liner shape
as the Verification section and point the manifest at a real CycloneSEQ /
PacBio FASTQ. The shared conda env is `env_geno_exec` per
`config/envs.yaml`; the orchestrator still runs in `gene_agent`.

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python - <<'PY'
from app.graph.builder import build_graph
from app.schemas.state import initial_state

state = initial_state(
    session_id="phase4-flye-quast-smoke",
    input_manifest_ref="<local-manifest-with-raw_long_reads>.yaml",
    data_dir="data/phase4_smoke",
)
state["stage_outputs"] = {
    "read_qc": {"execution_mode": "local"},
    "assembly": {
        "execution_mode": "local",
        "flye_parameters": {
            "read_type": "nano-raw",          # or nano-hq / pacbio-raw
            "estimated_genome_size": 2_000_000,
            "threads": 4,
        },
    },
    "assembly_review": {"execution_mode": "local"},
}

graph = build_graph()
result = graph.invoke(state, config={"configurable": {"thread_id": state["session_id"]}})
print("current_stage:", result.get("current_stage"))
print("supervision_state:", result.get("supervision_state"))
PY
```

Expected outcome when `env_geno_exec` has `flye` and `quast.py`:

- `data/artifacts/assembly/phase4-flye-quast-smoke/flye/draft_assembly.fasta`
- `data/artifacts/assembly_review/phase4-flye-quast-smoke/quast/<candidate>/quast_out/report.tsv`
- `data/artifacts/assembly_review/phase4-flye-quast-smoke/quast/<candidate>/quast_summary.json`
- `data/artifacts/assembly_review/phase4-flye-quast-smoke/assembly_review_request.json`
- `supervision_state == "blocked_human"` at `assembly_review`.

When `flye_parameters` are missing or incomplete the workflow stops at
`assembly` with `status: "blocked_param"` and a `missing_params` /
`invalid_params` list. When `env_geno_exec` is missing Flye or QUAST the
workflow stops at the corresponding stage with `status: "blocked_env"`
(env unusable) or `status: "missing_tool"` (env ok, tool absent), with an
`env_preflight_blocked` ledger event recording which tool was missing.

Note: the CLI `gene-agent run --local-exec read_qc ...` path does not
currently accept `flye_parameters` (no CLI surface for the parameters
dict yet). The Python one-liner is the runnable smoke shape until a
structured Flye-parameter entry point is added.

