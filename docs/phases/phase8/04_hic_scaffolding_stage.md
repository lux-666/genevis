# 08-04 Hi-C Scaffolding Stage Alignment

任务状态：进行中。此文件保留 Hi-C curation 主题，但不再描述新增的 assembly 内部 Hi-C 子代理。Hi-C 已经有独立 `hic_scaffolding` stage；Phase 8 只定义它和新增 `t2t` stage 的边界，以及未来可增强的 curation/workspace contract。

## 目标

把现有 `hic_scaffolding` stage 明确为 chromosome-level assembly candidate stage：

- 消费 `assembly_review` 产出的 final assembly。
- 在有 Hi-C 且目标需要 chromosome-level 时运行或规划 scaffolding。
- 保留 pre-Hi-C assembly 作为 fallback。
- 产出可供 `t2t` 或 annotation 使用的 final assembly ref。

## Boundary

`hic_scaffolding` 负责：

- Hi-C input validation。
- AutoHiC end-to-end planning or execution（内部使用 Juicer、3D-DNA 和深度学习模型）。
- chromosome FASTA、`.hic`、`.assembly`、HTML report refs。
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

## Current Method Execution

`app/stages/hic_scaffolding/method_application/` 现在与 assembly 的
`method_application/` 保持同一边界：stage 主图只负责输入、候选、review
gate 和 downstream contract；每个工具方法各自冻结命令，统一 lifecycle
负责 run、ledger 和 artifact collection。

**Implementation note**: 当前唯一方法：
- `hic_scaffolding.candidate.autohic` (in `method_application/autohic.py`)
- Lifecycle helpers in `method_application/lifecycle.py`

执行模式：
- 输入 adopted contig assembly、成对 Hi-C R1/R2、限制性内切酶、error model、chromosome model。
- adapter 复制 assembly，避免 AutoHiC 对上游 FASTA 做原地规范化。
- adapter 把 reads 规范为 AutoHiC 要求的 `rawdata/fastq/*_R1/_R2.fastq(.gz)`。
- AutoHiC 内部运行 Juicer → 3D-DNA → error detection/correction → 3D-DNA rerun → chromosome assignment → report。

每次执行都落盘 method-specific script 和 `FrozenAttemptSpec`，并登记
chromosome FASTA、`.hic`、`.assembly`、HTML report、stdout/stderr 与 GeneAgent
review bundle。执行失败时仍保留 pre-Hi-C assembly 作为 annotation fallback。

Planning and execution use separate contracts. The TUI may keep incomplete
method candidates in `needs_clarification` state, but `RunConfig` only accepts
execution-ready candidates. Input asset references stay at the stage level;
tool switches stay in the method parameter block:

```json
{
  "local_exec": ["hic_scaffolding"],
  "hic_scaffolding": {
    "methods": ["autohic"],
    "method": "autohic",
    "autohic_parameters": {
      "restriction_enzyme": "MboI",
      "error_model_ref": "/path/to/error_model.pth",
      "chromosome_model_ref": "/path/to/chr_model.pth",
      "threads": 40,
      "number_of_edit_rounds": 2
    }
  }
}
```

AutoHiC 自动识别染色体边界，因此 RunConfig 不再要求 `chromosome_count`。
`restriction_enzyme`、`error_model_ref`、`chromosome_model_ref` 缺失时 candidate
状态为 `needs_clarification`。

Expected output keys should stay under `stage_outputs.hic_scaffolding`:

```python
{
    "status": "completed | skipped | blocked_human | failed",
    "pre_hic_assembly_ref": "...",
    "final_assembly_for_annotation": "...",
    "selected_method_name": "hic_scaffolding.candidate.autohic",
    "notes": [],
}
```

**Implementation note**: `final_assembly_for_annotation` is the canonical Hi-C
handoff. T2T resolves it through the shared assembly resource spec.

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
    "hic_method": "autohic | user_curated | skipped",
}
```

## Tests

- `test_hic_scaffolding_remains_top_level_stage`
- `test_hic_scaffolding_consumes_assembly_review_final_ref`
- `test_hic_scaffolding_outputs_final_ref_for_t2t`
- `test_hic_scaffolding_chromosome_without_hic_blocks_or_skips_by_policy`
- `test_hic_scaffolding_does_not_write_stage_outputs_assembly_hic_curation`

**Test status**:
- 本地单元测试覆盖 planning、skip、fake AutoHiC success/failure、双模型与成对 reads 校验、四类输出登记和 review gate。
- 真实 AutoHiC/Juicer/3D-DNA 的 HPC real-data smoke 尚待运行。

## Acceptance

- No Phase 8 doc describes Hi-C curation as an assembly internal agent.
- Hi-C enhancements target `app/stages/hic_scaffolding`.
- Downstream T2T consumes the final Hi-C output when available.
