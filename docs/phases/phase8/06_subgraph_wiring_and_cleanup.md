# 08-06 Stage Wiring And Cleanup

任务状态：核心已实施。`t2t` stage 已接入 STAGE_ORDER 和 parent graph，assembly_dedup 位置已确认。Knowledge packs 迁移和架构统一（如 review gate 风格统一）见 DESIGN_ISSUES_AND_TODO.md backlog。

## 目标

1. 更新 `STAGE_ORDER`，插入 `t2t`。
2. 在 parent graph 中接入 `t2t_stage`。
3. 新建 `app/stages/t2t/`。
4. 清理 Phase 8 文档和测试中错误的额外输入画像 stage 命名。
5. 保持 `assembly -> assembly_review -> hic_scaffolding -> t2t -> repeat_annotation` contract 清晰。

## Compatibility With Recent Commits

Commit `ca685a9` is the current implementation baseline for Phase 8:

- `assembly` already has `resolve_extended_inputs -> infer_input_profile -> select_route`.
- `assembly/route_selection` is registered in the review gate registry.
- `AssemblyRunConfig` accepts `hifi_reads_ref`, `hic_reads_ref`, `target_level`, and `assembly_route`.
- The graph now returns to candidate planning and then enters the internal `method_application/` package. Flye and NextDenovo both have executable method graphs.

Do not undo this slice while adding `t2t`. The safe implementation order is:

1. Add `t2t` to `STAGE_ORDER`, run-config stage literals, parent graph, and a minimal stage skeleton.
2. Keep existing assembly route-selection tests passing.
3. Add inventory profile fields as hints.
4. Add additional method branches inside `method_application/` rather than adding more outer assembly nodes.
5. Migrate or alias assembly-scoped T2T prototypes once `t2t` owns canonical readiness artifacts.

Commit `eb1c838` only removes redundant function-annotation bundle metadata and aligns Phase 7 docs with real schema fields. It should not require any Phase 8 wiring changes.

## Target Stage Order

```python
STAGE_ORDER = [
    "inventory",
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "hic_scaffolding",
    "t2t",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
]
```

**Implementation note**: `assembly_dedup` 位于 `assembly` 和 `assembly_review` 之间，
在 STAGE_ORDER 中已存在。文档更新以反映实际 stage 顺序。

`inventory` 不重命名。所有用户可见文档也使用 `inventory`。

## Parent Graph Wiring

Update `app/graph/builder.py`:

```python
from app.stages.t2t.builder import build_stage as build_t2t_stage

graph.add_node("t2t_stage", build_t2t_stage())
```

Add routing target:

```python
"t2t_stage": "t2t_stage",
```

And include it in the loop that returns stage nodes to `route_controller`.

## New T2T Stage Files

```text
app/stages/t2t/
  __init__.py
  builder.py
  nodes.py
  routing.py
```

Minimum graph:

```text
enter
  -> resolve_input
  -> telomere_check
  -> gap_survey
  -> readiness
  -> final_gate
       completed     -> commit
       blocked_human -> block
       failed        -> fail
```

Keep first implementation small and deterministic. LLM review task generation can be added behind a fallback-safe node once schemas are stable.

## Assembly Cleanup

Assembly cleanup remains scoped to assembly:

- Keep `assembly` as the long-read candidate stage.
- Move Flye-specific execution into method-specific branch/subgraph.
- Do not route from assembly directly into Hi-C or T2T.
- Keep `commit_assembly_plan` output compatible with `assembly_review`.

Previous Flye-specific outer path:

```text
plan_candidate_attempts
freeze_flye_attempt
run_flye_attempt
collect_flye_artifacts
```

Current target shape:

```text
plan_candidate_bundles
method_application
  flye
  nextdenovo
  hifiasm_ont
  hifiasm_hifi
commit_assembly_plan
```

This can be implemented before or after the first `t2t` skeleton, but it must not introduce a new top-level stage.

## Hi-C Cleanup

Do not create `app/stages/assembly/hic_curation.py` or `stage_outputs.assembly.hic_curation`.

Enhancements belong under:

```text
app/stages/hic_scaffolding/
```

`hic_scaffolding` should expose a final assembly ref that `t2t` can consume. If code already uses `final_assembly_for_annotation`, add an alias only when needed and keep backward compatibility.

## Review Gates

Expected gates after Phase 8:

- `assembly.route_selection` if route selection remains human-gated.
- `assembly_review.assembly_review`.
- `hic_scaffolding.*` existing/expanded gate.
- `t2t.t2t_final_review`.

Do not add separate top-level gates for input profiling, long-read candidate selection, or Hi-C curation aliases; those are not new stages.

## Tests

- `test_stage_order_includes_t2t_after_hic_scaffolding`
- `test_parent_graph_routes_to_t2t_stage`
- `test_requested_stages_can_include_t2t`
- `test_inventory_name_remains_inventory`
- `test_no_alternate_profile_stage_name_in_stage_order`
- `test_hic_scaffolding_remains_top_level_stage`
- `test_t2t_stage_consumes_hic_or_assembly_review_final_ref`
- `test_assembly_review_downstream_contract_still_passes`

## Documentation Cleanup

After implementation:

- Update `docs/stage_contracts.md` with `t2t`.
- Update intent prompt docs to list `t2t` when users ask for T2T/near-complete/chromosome-end review.
- Update `docs/handoff.md` with current next task.
- Keep old alternate profile-stage references only in retrospective notes, never as active stage names.

## Acceptance

- `t2t` is a real top-level stage.
- `inventory` remains the first stage and is not renamed.
- Hi-C remains `hic_scaffolding`.
- Assembly method refactor does not change downstream stage order.
- Docs no longer describe Phase 8 as assembly-internal input-profile/Hi-C/T2T agents.
