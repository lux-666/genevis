# 08-04 Hi-C Scaffolding Stage Alignment

任务状态：待重整。此文件保留 Hi-C curation 主题，但不再描述新增的 assembly 内部 Hi-C 子代理。Hi-C 已经有独立 `hic_scaffolding` stage；Phase 8 只定义它和新增 `t2t` stage 的边界，以及未来可增强的 curation/workspace contract。

## 目标

把现有 `hic_scaffolding` stage 明确为 chromosome-level assembly candidate stage：

- 消费 `assembly_review` 产出的 final assembly。
- 在有 Hi-C 且目标需要 chromosome-level 时运行或规划 scaffolding。
- 保留 pre-Hi-C assembly 作为 fallback。
- 产出可供 `t2t` 或 annotation 使用的 final assembly ref。

## Boundary

`hic_scaffolding` 负责：

- Hi-C input validation。
- YaHS / future scaffolding method planning or execution。
- contact-map/manual curation workspace refs。
- human selection gate。
- final chromosome-scaffolded candidate ref。

`hic_scaffolding` 不负责：

- long-read assembly candidate generation。
- assembly_review 的 QUAST/BUSCO/selection。
- telomere/gap/T2T readiness。
- repeat/gene/function annotation。

## Existing Stage Contract

当前代码已经有：

```text
app/stages/hic_scaffolding/
  builder.py
  nodes.py
  routing.py
```

Phase 8 不新建 parallel stage。若要增强 Hi-C curation，也应该在这些文件下演进。

Expected output keys should stay under `stage_outputs.hic_scaffolding`:

```python
{
    "status": "completed | skipped | blocked_human | failed",
    "pre_hic_assembly_ref": "...",
    "chromosome_scaffolded_assembly_candidate_ref": "...",
    "final_assembly_for_annotation": "...",
    "final_assembly_for_t2t_or_annotation": "...",
    "fallback_assembly_ref": "...",
    "selected_method_name": "hic_scaffolding.candidate.yahs | ...",
    "notes": [],
}
```

`final_assembly_for_t2t_or_annotation` may be an alias added for clarity; downstream can fall back to existing `final_assembly_for_annotation` until code is migrated.

## Routing With T2T

After `assembly_review`:

- If target level is `chromosome`, `near_t2t`, or `strict_t2t` and Hi-C is available, run/enter `hic_scaffolding`.
- If Hi-C is unavailable and target requires chromosome-level evidence, block or request data according to policy.
- If Hi-C is skipped and target is near/strict T2T, `t2t` may still run on the assembly_review final assembly, but must record lower-confidence provenance.

After `hic_scaffolding`:

- If target level is `near_t2t` or `strict_t2t`, route to `t2t`.
- Otherwise route to `repeat_annotation`.

## Curation Workspace Extension

Optional future artifacts belong to `hic_scaffolding`:

```python
HIC_SCAFFOLDING_WORKSPACE = "hic_scaffolding.workspace"
HIC_SCAFFOLDING_CANDIDATE = "hic_scaffolding.candidate"
HIC_SCAFFOLDING_DECISION = "hic_scaffolding.decision"
HIC_SCAFFOLDING_RECHECK_SUMMARY = "hic_scaffolding.recheck_summary"
```

Candidate provenance should preserve upstream assembly details:

```python
{
    "source_stage": "assembly_review",
    "pre_hic_assembly_ref": "...",
    "assembly_candidate_ref": "...",
    "assembly_method": "flye | hifiasm_ont | hifiasm_hifi | unknown",
    "hic_method": "yahs | user_curated | skipped",
}
```

## Tests

- `test_hic_scaffolding_remains_top_level_stage`
- `test_hic_scaffolding_consumes_assembly_review_final_ref`
- `test_hic_scaffolding_outputs_final_ref_for_t2t`
- `test_hic_scaffolding_chromosome_without_hic_blocks_or_skips_by_policy`
- `test_hic_scaffolding_does_not_write_stage_outputs_assembly_hic_curation`

## Acceptance

- No Phase 8 doc describes Hi-C curation as an assembly internal agent.
- Hi-C enhancements target `app/stages/hic_scaffolding`.
- Downstream T2T consumes the final Hi-C output when available.
