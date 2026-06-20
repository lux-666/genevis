# 08-03 Assembly Stage Methods

任务状态：部分实施。当前 `assembly` stage 正在从 Flye-first path 升级为 data-type-driven long-read candidate stage；方法调用已经下沉到内部 `method_application/` package，其中 Flye 和 NextDenovo 都已经接入 executable graph。

## 目标

让 `assembly` stage 负责：

- 根据 `inventory` 和 run-config hints 选择 assembly route / method。
- 规划 primary/fallback candidate。
- 把具体方法应用下沉到 method-specific subgraph。
- 产出稳定 candidate bundle 和 optional draft assembly ref。

核心设计点：Flye 只是一个方法，不应该写死在 assembly 主干。Assembly 主干应该是 `resolve inputs -> select method -> apply selected method(s) -> collect candidate refs`。

## Current Implementation Compatibility

Commit `ca685a9` changed the assembly builder to:

```text
enter
  -> resolve_extended_inputs
  -> infer_input_profile
  -> select_route
  -> build_assembly_candidate_set
  -> select_primary_and_fallback
  -> plan_candidate_attempts
  -> optional freeze_flye_attempt/run_flye_attempt/collect_flye_artifacts
  -> commit
```

This was compatible with the new design because only the first route-selection slice was added; the method-application part was still the old Flye-specific path. The first refactor slice has now replaced the outer Flye path with one internal method-application subgraph.

In other words:

- Keep `resolve_extended_inputs`, `infer_input_profile`, and `select_route` until inventory hints are ready to replace or feed them.
- Treat `build_assembly_candidate_set`, `select_primary_and_fallback`, and `plan_candidate_attempts` as the current candidate-planning compatibility layer.
- Keep `freeze_flye_attempt`, `run_flye_attempt`, and `collect_flye_artifacts` as Flye method nodes, but route to them through `app/stages/assembly/method_application/flye.py`.
- Keep `freeze_nextdenovo_attempt`, `run_nextdenovo_attempt`, and `collect_nextdenovo_artifacts` as NextDenovo method nodes, routed through `app/stages/assembly/method_application/nextdenovo.py`.

## Boundary

`assembly` 负责：

- long-read assembly candidate planning。
- Flye / hifiasm / NextDenovo / Canu 等方法的 dispatch。
- method-specific freeze/run/collect。
- candidate provenance。

`assembly` 不负责：

- `inventory` 输入画像。
- `assembly_review` 质量评估和人工选择。
- `hic_scaffolding` chromosome scaffolding。
- `t2t` telomere/gap/readiness review。

## Method Application Subgraph

建议把当前主 graph 中的 Flye 执行 path 下沉为内部 subgraph：

```text
assembly
  enter
  resolve_assembly_inputs
  select_assembly_methods
  plan_candidate_bundles
  method_application
    dispatch_by_method
      flye
        freeze
        run
        collect
      hifiasm_ont
        freeze
        run
        collect
      hifiasm_hifi
        freeze
        run
        collect
      nextdenovo
        freeze
        run
        collect
      canu
        freeze/planning-only initially
  commit_assembly_plan
```

Current implementation:

```text
assembly outer graph
  -> plan_candidate_attempts
  -> method_application
       dispatch
         flye -> flye graph
           freeze -> run -> collect
         nextdenovo -> nextdenovo graph
           freeze -> run -> collect
         done
  -> commit_assembly_plan
```

The subgraph should return compact updates into `stage_outputs.assembly`, not leak rich execution state into downstream stages.

## MVP Methods

| Route | Method | Status | Notes |
|---|---|---|---|
| `cyclone` | `assembly.candidate.flye` | executable | current real path, moved under method subgraph |
| `ont` | `assembly.candidate.hifiasm_ont` | executable or first new slice | hifiasm `--ont` |
| `hifi` | `assembly.candidate.hifiasm_hifi` | executable or first new slice | hifiasm default mode; do not use nonexistent `--hifi` flag |
| `ont` / fallback | `assembly.candidate.flye` | executable | optional baseline |
| `ont`/`hifi` | `assembly.candidate.nextdenovo` | executable | local NextDenovo path uses run.cfg + frozen attempt + draft assembly artifact |
| `ont`/`hifi` | `canu` | planning/future | keep descriptors and compatibility checks |

## Route-Specific Reads

When multiple long-read types exist, method inputs must be selected by route/method, not blindly by `primary_long_reads_ref`:

```python
def reads_ref_for_method(method_name, assembly_out, inventory_out):
    if method_name == "assembly.candidate.hifiasm_hifi":
        return assembly_out.get("hifi_reads_ref") or _asset_ref("pacbio_hifi")
    if method_name in {"assembly.candidate.flye", "assembly.candidate.hifiasm_ont"}:
        return assembly_out.get("ont_reads_ref") or assembly_out.get("primary_long_reads_ref")
    return None
```

If route/method inputs are missing, block or mark that candidate incompatible. Do not silently switch an ONT route to HiFi reads.

## Candidate Bundle Contract

Each selected method writes a candidate bundle:

```python
{
    "candidate_id": "<session>:assembly.candidate.flye",
    "method_name": "assembly.candidate.flye",
    "status": "planned | executed | failed",
    "input_ref": "...",
    "input_type": "clean_long_reads | long_reads.fofn | hifi_reads",
    "expected_outputs": {"draft_assembly_fasta": "..."},
    "logs": {"primary_log": "..."},
    "metadata": {
        "assembly_route": "cyclone | ont | hifi",
        "assembly_method": "flye | hifiasm_ont | hifiasm_hifi",
        "method_application_subgraph": "assembly.method.flye",
        "draft_assembly_ref_status": "planned_path_not_materialized | materialized",
    },
}
```

`commit_assembly_plan` should preserve the existing downstream keys where possible:

- `candidate_methods`
- `candidate_plans`
- `selected_methods`
- `selected_candidate_refs`
- `candidate_artifact_refs`
- `draft_assembly_ref` when a method actually materializes one
- `candidate_assembly_refs`

## Existing Code Migration

Current path:

```text
build_assembly_candidate_set
select_primary_and_fallback
plan_candidate_attempts
freeze_flye_attempt
run_flye_attempt
collect_flye_artifacts
```

Implemented target path:

```text
select_assembly_methods
plan_candidate_bundles
method_application_subgraph
commit_assembly_plan
```

Migration rule:

- Move Flye parameter validation, script generation, frozen attempt execution, and artifact collection into the Flye method branch.
- Add NextDenovo parameter validation, run.cfg generation, frozen attempt execution, and artifact collection in the NextDenovo method branch.
- Keep helper behavior and tests, but name around method application instead of Flye-specific stage routing.
- Do not break `assembly_review` committed keys.

## Tests

- `test_assembly_default_flye_candidate_still_plans`
- `test_assembly_local_flye_runs_under_method_subgraph`
- `test_assembly_local_nextdenovo_runs_under_method_subgraph`
- `test_assembly_hifiasm_ont_uses_ont_reads`
- `test_assembly_hifiasm_hifi_uses_hifi_reads_without_hifi_flag`
- `test_assembly_method_dispatch_rejects_missing_route_specific_reads`
- `test_assembly_method_failure_does_not_leak_frozen_attempt_state_after_commit`
- `test_assembly_review_committed_keys_remain_stable`

## Acceptance

- Assembly mainline is no longer Flye-specific.
- Flye execution behavior remains covered after moving into method subgraph.
- NextDenovo can be selected by run config and executed through the same method-application package.
- New methods can be added without changing top-level stage order.
- `assembly` remains the only long-read candidate stage.
