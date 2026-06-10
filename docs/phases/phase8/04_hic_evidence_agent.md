# 08-04 HiCEvidenceAgent

任务状态：待实施。

## 目标

当 LongReadCandidateAgent 的 decision gate 输出 `enter_hic`（且 `has_hic=True`）时，HiCEvidenceAgent 运行 Hi-C reads 比对和错连检测，给出是否需要进入 T2T finishing 的判断。职责是**质量验证**，不是染色体支架化。

## 前置

task 03 完成：`stage_outputs.assembly.status == "hic_evidence_required"`，`hic_reads_ref` 已解析。`has_hic=False` 时此子代理完全跳过（路由在 task 03 的 Python 层已处理）。

## 与现有 hic_scaffolding stage 的边界

| | HiCEvidenceAgent（本任务） | hic_scaffolding stage（现有）|
|---|---|---|
| 位置 | assembly stage 内部 | 独立 stage |
| 输入 | 候选 draft assembly + Hi-C reads | assembly_review 通过后的 final assembly |
| 目的 | 检测错连，验证组装质量 | 染色体级支架化，注释前置 |
| 输出 | 错连报告 + 是否推进 T2T | scaffolded assembly |

## 工具选择

| 步骤 | 工具 | 方式 |
|---|---|---|
| Hi-C 比对 | minimap2 `-x sr` | registry 已有 minimap2，freeze/run/collect |
| 比对统计 | samtools flagstat | 直接 subprocess（轻量） |
| 错连检测 | YaHS check / break evidence | `yahs --check` 需先验证是否受当前版本支持；若不支持，运行 YaHS 到临时目录并解析 break AGP |

## 节点实现：`app/stages/assembly/nodes.py`

### `freeze_hic_alignment`

- 输入：`selected_candidate_id` 对应的 primary fasta + `hic_reads_ref`
- Hi-C reads 解析：`_parse_hic_read_paths` 私有辅助（约 20 行），支持 paired fastq 或 fofn
- shell script：
  ```bash
  minimap2 -ax sr -t {threads} {assembly.fasta} {hic_r1.fastq} {hic_r2.fastq} \
    | samtools sort -o {hic_sorted.bam}
  samtools index {hic_sorted.bam}
  ```
- `expected_outputs = [hic_sorted.bam, hic_sorted.bam.bai]`
- 状态 → `hic_alignment_frozen`

### `run_hic_alignment` / `collect_hic_alignment`

标准 `run_frozen_attempt` 调用；collect 验证 BAM 存在，注册 artifact。状态 → `hic_aligned`。

### `evaluate_hic_evidence`

加载 `load_skill_context("t2t_criteria")`，直接 subprocess：

1. `samtools flagstat {hic_sorted.bam}` → 解析比对率
2. 若 YaHS 可用：先验证当前版本是否支持 `yahs --check`。若支持，运行 `yahs --check {assembly.fasta} {hic_sorted.bam}` 并解析 breakpoint 数量；若不支持，在临时目录运行 YaHS 常规流程，解析 `*_initial_break_*.agp` / `*_break.agp` 作为 misassembly evidence。
3. 根据 `ctx["rules"]["hic_gates"]` 分级 `hic_quality`

写入 `stage_outputs.assembly.hic_evidence`：
```python
{
    "alignment_rate": float,
    "misassembly_breakpoints": int | None,  # yahs 不可用时为 None
    "hic_quality": "high" | "medium" | "low",
    "notes": list[str],
}
```

### `hic_decision_gate`

**纯 Python，不调 LLM**——Hi-C 错连是数值判断，rules 提供所有需要的边界。

```python
def hic_decision_gate(state: TopLevelState) -> dict:
    ctx = load_skill_context("t2t_criteria")
    hic = assembly_out.get("hic_evidence", {})
    target = assembly_out.get("target_level", "draft")

    misassemblies = hic.get("misassembly_breakpoints") or 0
    hic_quality = hic.get("hic_quality", "low")
    max_misassembly = ctx["rules"].get("hic_gates", {}).get("max_misassembly_for_near_t2t", 10)

    # 安全边界：错连过多，强制 block
    if misassemblies > max_misassembly:
        return _block_for_human(state, f"hi_c_detected_{misassemblies}_misassembly_breakpoints")

    # 高 Hi-C 质量只在 near_t2t / strict_t2t 目标时触发 T2T finishing
    if target in ("near_t2t", "strict_t2t"):
        return _update_assembly_output(state, {"status": "t2t_finishing_required"})

    # draft 目标：无论 Hi-C 质量高低，不强制进 T2T
    return _update_assembly_output(state, {"status": "draft_ready"})
```

**为什么 draft 目标的 high Hi-C quality 不触发 T2T finishing**：用户明确要 draft，系统不应该因为"Hi-C 质量好"就自动升级目标。知识库可以在 TUI 中提示用户"Hi-C 质量好，可以考虑升级到 near-T2T 目标"，但路由决策尊重显式 target_level。

### Gate 注册（`app/review/gates.py`）

```python
register_review_gate(ReviewGateDeclaration(
    stage="assembly",
    gate_id="hic_misassembly_review",
    decisions_allowed=("accept_misassemblies", "adopt_draft_without_hic", "stop_human_review"),
    decision_applier="app.stages.assembly.nodes:apply_hic_misassembly_review",
    prompt_adapter="generic",
    template_preferred_decision="adopt_draft_without_hic",
))
```

`apply_hic_misassembly_review`：
- `accept_misassemblies` → 忽略错连，继续（`target_level in near_t2t/strict_t2t → t2t_finishing_required`，否则 `draft_ready`）
- `adopt_draft_without_hic` → 忽略 Hi-C 结果，`draft_ready`

## `t2t_criteria` Knowledge Pack

### `t2t_criteria/rules.yaml`

```yaml
pack_id: assembly.t2t_criteria
version: "1.0"

allowed_verdicts:
  - adopt_draft
  - near_t2t
  - strict_t2t_candidate
  - insufficient
  - request_data

allowed_human_decisions:
  - adopt_draft
  - adopt_near_t2t
  - adopt_strict_t2t
  - request_more_data
  - stop_human_review

hic_gates:
  max_misassembly_for_near_t2t: 10
  quality_tiers:
    high:
      min_alignment_rate: 0.85
      max_misassembly: 5
    medium:
      min_alignment_rate: 0.70
      max_misassembly: 20
    low:
      min_alignment_rate: 0.0
      max_misassembly: 999

safety_boundaries:
  never_auto_adopt_strict_t2t: true
```

### `t2t_criteria/SKILL.md`

```markdown
# T2T Criteria Skill

## Hi-C Evidence Interpretation

Hi-C misassembly breakpoints are signals, not verdicts. Before escalating:

1. **Check breakpoint location**: Breakpoints in pericentromeric regions or
   rDNA arrays are expected and do not indicate assembly errors. YaHS break
   evidence does not distinguish these from true misassemblies.

2. **Alignment rate context**: < 70% alignment rate may mean the Hi-C library
   had quality issues rather than the assembly being wrong. Check if the reads
   map to a reference genome (if available) before concluding the assembly is
   misassembled.

3. **Low breakpoint count with low alignment rate**: This ambiguous pattern
   warrants block_human rather than either auto-adopt or auto-escalate.

## When to Use `adopt_draft_without_hic`

When Hi-C evidence is ambiguous and the user's target is draft, it is
reasonable to proceed without resolving Hi-C issues. Document in notes
that Hi-C validation was inconclusive.
```

## 测试

- `test_freeze_hic_alignment_paired_fastq`：正确生成包含双 fastq 的 minimap2 命令。
- `test_collect_hic_alignment_missing_bam`：BAM 不存在 → `status=failed`。
- `test_evaluate_hic_evidence_quality_tiers`：比对率 0.90 → high；0.60 → low；yahs 不可用 → `misassembly_breakpoints=None`，notes 记录。
- `test_evaluate_hic_evidence_yahs_check_fallback`：当前 YaHS 不支持 `--check` 时，运行临时目录常规流程并解析 `*_initial_break_*.agp` / `*_break.agp`。
- `test_hic_decision_gate_draft_target`：target=draft + high Hi-C quality → `draft_ready`（不触发 T2T）。
- `test_hic_decision_gate_near_t2t_target`：target=near_t2t + low misassembly → `t2t_finishing_required`。
- `test_hic_decision_gate_misassembly_blocks`：misassemblies > 10 → `blocked_human`，无论 target_level。
- `test_hic_decision_gate_no_pack`：pack 不存在时使用默认阈值，不崩溃。

## 验收

- fake BAM → artifact 注册。
- `hic_evidence` 写入 `stage_outputs.assembly`，包含 `hic_quality`。
- draft target + high Hi-C quality → `draft_ready`（测试验证不进 T2T finishing）。
- `has_hic=False` 时此子代理节点完全不出现在路由中（task 06 的图连线保证）。
- `hic_misassembly_review` gate 已注册，task 06 的验收测试中验证。
