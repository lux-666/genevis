# 02 Read QC SeqKit Stats

Task status: implemented.

## Goal

Implement the first real method slice:

```text
raw_long_reads
-> read_qc.long_read_stats
-> seqkit stats
-> read stats artifact
-> ledger event
```

SeqKit is the first executable method. NanoPlot and pyfastx remain candidates,
not Phase 2 execution targets.

## Files

- Modify: `app/stages/read_qc/nodes.py`
- Modify: `app/stages/read_qc/builder.py`
- Modify: `app/stages/read_qc/routing.py`
- Modify: `app/schemas/commands.py`
- Modify: `app/schemas/artifacts.py`
- Modify: `app/execution/local_runner.py`
- Modify: `app/execution/artifacts.py`
- Modify: `app/control/ledger.py`
- Add: `descriptors/packages/seqkit.yaml`
- Add: `descriptors/methods/read_qc/seqkit_stats.yaml`
- Modify: `descriptors/registry.yaml`
- Test: `tests/test_read_qc.py`
- Keep baseline: `tests/test_manifest.py`, `tests/test_inventory.py`,
  `tests/test_project_scaffold.py`

Use existing runner, artifact, ledger, and descriptor modules. Do not create a
parallel execution framework.

## Project Context

`read_qc` is currently a placeholder stage built by
`app/stages/factory.build_placeholder_stage("read_qc")`. This task is the first
place where that placeholder should be replaced by a real subgraph.

Existing project pieces to reuse:

- `app/schemas/descriptors.py`: `PackageDescriptor`, `MethodDescriptor`,
  `FrozenAttemptSpec`, `DescriptorRegistry`
- `app/schemas/artifacts.py`: `ArtifactMeta`
- `app/execution/local_runner.py`: current local runner boundary
- `app/execution/artifacts.py`: artifact index helper
- `app/control/ledger.py`: JSONL event writer
- `descriptors/registry.yaml`: package/method registry root

## Node Plan

Replace the placeholder read QC graph with explicit nodes:

```text
enter_read_qc
-> resolve_raw_long_reads
-> plan_seqkit_stats
-> freeze_seqkit_attempt
-> run_seqkit_stats
-> collect_seqkit_artifacts
-> commit_read_qc
```

Suggested node ownership:

- `resolve_raw_long_reads`: load `asset_registry_ref`, find exactly the
  `raw_long_reads` input assets, fail terminally if missing.
- `plan_seqkit_stats`: load descriptors and build method intent.
- `freeze_seqkit_attempt`: produce deterministic execution spec with working
  directory and expected outputs.
- `run_seqkit_stats`: call the existing local runner only when local execution is
  explicitly enabled.
- `collect_seqkit_artifacts`: create stats/stdout/stderr/log artifact records and
  ledger facts.
- `commit_read_qc`: advance to `genome_survey` with refs and compact summary.

Keep routing deterministic. No LLM or human gate is needed for SeqKit stats.

## Reuse From `rna_agent`

Directly reusable patterns:

- descriptor -> frozen attempt -> local runner -> artifact collect -> ledger;
- separate execution records from graph state;
- artifact collection fails when expected outputs are missing;
- local runner writes stdout/stderr/log under the working directory;
- tests for success, failed process, missing output, and ledger append.

Do not copy:

- Harmony script generation;
- h5ad bundle fields;
- annotation/benchmark stage names.

## Optimization Notes

- Prefer one small FASTQ fixture over mocked stats parsing for the success path.
- Preserve raw SeqKit output even if a compact summary parser is added.
- Keep multi-FASTQ behavior simple first: one attempt can accept the manifest's
  declared files only if the command template explicitly supports them. Otherwise
  stop and ask before inventing FOFN semantics here.

## Method Contract

Stage:

- `read_qc`

Substep:

- `long_read_stats`

Executable method:

- `read_qc.long_read_stats.seqkit_stats`

Candidates documented for later:

- SeqKit stats
- NanoPlot
- pyfastx

Required input:

- `raw_long_reads` resolved from the asset registry

Outputs:

- read stats artifact
- stdout artifact
- stderr artifact
- execution log artifact if runner creates one
- ledger attempt events
- ledger artifact registration event

Stats should preserve or parse:

- read count
- total bases
- N50 when available
- length summary
- quality summary when SeqKit output provides it

## Tasks

- [x] Add SeqKit package descriptor.
- [x] Add `read_qc.long_read_stats.seqkit_stats` method descriptor.
- [x] Add descriptor registry tests for SeqKit package and method.
- [x] Resolve `raw_long_reads` from `asset_registry_ref`.
- [x] Build a frozen attempt spec before execution.
- [x] Execute `seqkit stats` only in explicit local execution mode.
- [x] Write read stats output under the artifact directory.
- [x] Record stdout/stderr/log artifact records.
- [x] Record ledger events for attempt started, succeeded/failed, and artifact
  registered.
- [x] Keep graph state limited to artifact refs and compact summaries.

## Implemented State

- `app/stages/read_qc/builder.py` now builds a real read QC subgraph instead of
  the generic placeholder.
- `app/stages/read_qc/nodes.py` resolves one `raw_long_reads` asset, plans
  `read_qc.long_read_stats.seqkit_stats`, freezes a `FrozenAttemptSpec` for
  `seqkit stats -a -T`, runs the local attempt when `execution_mode` is
  `local`, collects artifacts, and advances to `genome_survey`.
- `descriptors/packages/seqkit.yaml`,
  `descriptors/methods/read_qc/seqkit_stats.yaml`, and
  `descriptors/registry.yaml` register the SeqKit package and method.
- `app/execution/local_runner.py` has a minimal subprocess-backed local runner
  for frozen attempts.
- `app/execution/artifacts.py` appends artifact metadata under
  `data/artifacts/{session_id}/artifacts.jsonl`.
- `app/control/ledger.py` records attempt start, success, failure, and artifact
  registration events.
- `tests/test_read_qc.py` covers descriptor loading, missing registry,
  missing `raw_long_reads`, fake-SeqKit success, compact state summaries with
  total bases, N50, Q20/Q30, AvgQual, and missing SeqKit failure behavior.

改动要点：

- `app/stages/read_qc/builder.py` 将 `read_qc` 从 placeholder 换成真实 SeqKit stats
  子图。
- `app/stages/read_qc/nodes.py` 负责解析 `raw_long_reads`、构建
  `read_qc.long_read_stats.seqkit_stats` attempt、按显式 local execution 运行、收集
  stats/stdout/stderr/log artifacts，并写 compact summary。
- `app/execution/local_runner.py` 提供最小 subprocess runner；`app/execution/artifacts.py`
  追加 session artifact index；`app/control/ledger.py` 记录 attempt 和 artifact 事件。
- `descriptors/packages/seqkit.yaml`、`descriptors/methods/read_qc/seqkit_stats.yaml`
  和 `descriptors/registry.yaml` 注册 SeqKit package/method。
- `tests/test_read_qc.py` 覆盖 descriptor 加载、缺 registry/缺 `raw_long_reads`、fake
  SeqKit 成功、缺 binary/输出缺失等失败路径。

## Failure Behavior

- Missing asset registry: fail terminally with structured error.
- Missing `raw_long_reads`: fail terminally with structured error.
- Missing SeqKit binary: record failed attempt, do not write success artifact.
- Non-zero process exit: record failed attempt and stderr/log artifacts.
- Missing expected stats output: fail artifact collection distinctly from process
  failure.

## Non-Goals

- Do not run NanoPlot.
- Do not run pyfastx.
- Do not filter reads.
- Do not generate FOFN.
- Do not run genome survey, assembly, annotation, retry, or remote execution.
- Do not add pass/fail thresholds for stats.

## Acceptance Criteria

- A tiny FASTQ fixture can produce a SeqKit stats artifact.
- Attempt and artifact facts are inspectable through normal files.
- Graph state stores only refs and compact summaries.
- All failure paths are tested.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

If SeqKit is not installed in the target environment, stop and ask before
choosing a fallback.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
