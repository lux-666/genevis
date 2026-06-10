# 08-05 T2TFinishingAgent

任务状态：待实施。

## 目标

T2TFinishingAgent 整合候选 assembly、Hi-C evidence（如有）、telomere/gap 检测结果，通过 LLM + skill context 综合评估 T2T readiness，生成专家可直接行动的 review task 列表，并强制进入 human final gate（strict-T2T 必须人工确认）。

## 前置

以下任一状态进入：
- task 03 `decision_gate` 输出 `t2t_finishing_required`
- task 04 `hic_decision_gate` 输出 `t2t_finishing_required`

## 工具

| 检查 | 工具 | 调用方式 |
|---|---|---|
| 端粒信号 | tidk | 新增 package descriptor，subprocess |
| gap 统计 | seqkit | 已有，subprocess |

tidk 需新增 `descriptors/packages/tidk.yaml`（格式参照 `seqkit.yaml`）和 `registry.yaml` packages 条目。不需要 method descriptor——tidk 以 subprocess 方式调用。

## 节点实现

### `run_telomere_check`

```python
def run_telomere_check(state: TopLevelState) -> dict:
    # 确定输入 fasta（selected_candidate_id 对应的 primary fasta）
    # subprocess: tidk explore --minimum 5 --maximum 12 <assembly.fasta> > tidk_out.tsv
    # 解析 stdout TSV：找出频率最高的 motif + 在 contig 末端出现的比率
    # tidk 不可用 → notes 记录，telomere_detected=None，不 fail
    # 写入 stage_outputs.assembly.telomere_result:
    #   {motif, detected_at_n_ends, total_contig_ends, telomere_coverage_pct}
```

### `run_gap_survey`

```python
def run_gap_survey(state: TopLevelState) -> dict:
    # subprocess: seqkit locate -p "N+" <assembly.fasta>
    # 统计行数（gap 数）、最大 gap 长度、总 gap 碱基数
    # 写入 stage_outputs.assembly.gap_result:
    #   {gap_count, total_gap_bases, longest_gap_bp}
```

### `generate_review_tasks`

LLM 节点。加载 `load_skill_context("t2t_criteria")` + `load_skill_context("review_task_guide")`。

构建 prompt context：
```
[System]
You are a T2T genome quality expert.
{t2t_ctx["skill_text"]}
{review_ctx["skill_text"]}

[Rules]
{t2t_ctx["rules"]}
{review_ctx["rules"]}

[Past cases]
{t2t_ctx["cases"]}

[Allowed verdicts]
{t2t_ctx["rules"]["allowed_verdicts"]}   # ["adopt_draft","near_t2t","strict_t2t_candidate","insufficient","request_data"]

[Tool outputs]
telomere_result: {telomere_result}
gap_result: {gap_result}
hic_evidence: {hic_evidence or "not available"}
qc_summaries: {qc_summaries}
input_profile: {input_profile summary}
target_level: {target_level}
```

LLM 必须输出：
```json
{
  "t2t_verdict": "near_t2t | strict_t2t_candidate | insufficient | ...",
  "confidence": 0.78,
  "reasoning": "...",
  "uncertainty_notes": ["..."],
  "tasks": [
    {
      "priority": "high | medium | low",
      "category": "telomere | gap | misassembly | coverage | ...",
      "finding": "...",
      "action": "...",
      "can_auto_resolve": false
    }
  ],
  "blocking_for_strict_t2t": ["telomere_incomplete", ...]
}
```

**LLM 失败降级**：调用失败时用 `review_task_guide/rules.yaml` 中的 `fallback_tasks` 列表生成 tasks，`t2t_verdict = "insufficient"`，`confidence = 0.0`。不 fail 整个流程，进入 `t2t_final_gate`。

写入两个 artifact：
- `AssemblyT2TReadiness`（`data/artifacts/assembly/<session>/t2t_readiness.json`）：保存 verdict、confidence、reasoning、uncertainty_notes、blocking_reasons 和关键 QC 数值。
- `AssemblyReviewTasks`（`data/artifacts/assembly/<session>/t2t_review_tasks.json`）：保存专家可执行的 task 列表。

### `t2t_final_gate`

**强制 human gate，strict-T2T 路径无论 policy 都必须阻断。**

near-T2T auto 例外：若 `target_level != "strict_t2t"` 且 `t2t_verdict == "near_t2t"` 且 `confidence >= 0.8` 且 policy 为 `auto` → 直接采纳，不阻断。

near-T2T auto 采纳时必须在返回 `draft_ready` 前设置 `stage_outputs.assembly.draft_assembly_ref` 为选中候选的 primary fasta，并记录 `t2t_auto_adopted=True` / `t2t_decision="adopt_near_t2t"`；否则后续 `commit_assembly_plan` 会缺少草图组装引用。

Gate 注册（`app/review/gates.py`）：

```python
register_review_gate(ReviewGateDeclaration(
    stage="assembly",
    gate_id="t2t_final_review",
    decisions_allowed=(
        "adopt_draft",
        "adopt_near_t2t",
        "adopt_strict_t2t",
        "request_more_data",
        "stop_human_review",
    ),
    decision_applier="app.stages.assembly.nodes:apply_t2t_final_review",
    prompt_bundle="assembly.t2t_criteria",
    prompt_adapter="generic",
    template_preferred_decision="adopt_near_t2t",
    required_resume_fields=("selected_candidate_id", "t2t_decision"),
))
```

`apply_t2t_final_review`（约 25 行）：
- 读 `selected_candidate_id` 和 `t2t_decision`（`adopt_draft` / `adopt_near_t2t` / `adopt_strict_t2t` / `request_more_data`）
- 将 human decision 映射为 readiness verdict，并更新 `AssemblyT2TReadiness`
- `request_more_data` 不设置 `draft_assembly_ref`，继续 block_human / data review
- 采纳类 decision 设置 `draft_assembly_ref` 为选中候选的 primary fasta
- 采纳类 decision 状态 → `draft_ready`

`t2t_decision` 使用 human gate 的 `allowed_human_decisions`；不要与 LLM 输出的 `allowed_verdicts` 混用。映射关系为：`adopt_near_t2t` → readiness `t2t_verdict="near_t2t"`，`adopt_strict_t2t` → `strict_t2t_candidate`，`request_more_data` → `request_data`。

## Knowledge Pack：`review_task_guide`

### `review_task_guide/rules.yaml`

```yaml
pack_id: assembly.review_task_guide
version: "1.0"

task_categories:
  telomere:
    strict_t2t_required_coverage: 0.80
    near_t2t_acceptable_coverage: 0.60
  gap:
    near_t2t_max_count: 500
    strict_t2t_max_count: 50
  misassembly:
    near_t2t_max_breakpoints: 20
    strict_t2t_max_breakpoints: 5

fallback_tasks:
  - priority: high
    category: manual_review
    finding: "自动 review task 生成失败，请人工查看 QC 报告"
    action: "检查 telomere_result、gap_result 和 qc_summaries artifact"
    can_auto_resolve: false
```

### `review_task_guide/SKILL.md`

```markdown
# Review Task Generation Skill

## How to Generate Useful Review Tasks

A good review task tells the expert exactly what to look at and why —
it is not a restatement of the QC number.

**Template for telomere findings**:
- Bad: "Telomere coverage is 67%."
- Good: "67% of contig ends have TTAGGG signal. Check whether the missing
  33% corresponds to centromeric contigs (expected) or chromosome-arm
  termini (problematic). If all chromosome-arm termini have signal, the
  assembly is likely complete."

**Template for gap findings**:
- Bad: "There are 23 gaps."
- Good: "23 N-gaps totaling 46 kb. The largest (8 kb) is on chr3q. Verify
  whether this region corresponds to a known tandem repeat or rDNA cluster.
  If yes, the gap is acceptable for near-T2T; if not, targeted long-read
  sequencing may be needed."

## Uncertainty and Confidence

Set `confidence` to reflect genuine uncertainty, not optimism.
- Confidence < 0.6: `block_human`, even if numbers look good.
- Missing telomere data (tidk unavailable): cap confidence at 0.7 for near-T2T,
  0.5 for strict-T2T claims.
- Missing Hi-C evidence when target is strict-T2T: note this explicitly and
  reduce confidence accordingly.

## Strict T2T Requirements

Never return `strict_t2t_candidate` unless:
1. Telomere coverage ≥ 80%.
2. Gap count < 50.
3. BUSCO ≥ 98% (if available).
4. No unresolved misassemblies from Hi-C (if Hi-C was run).

If any of these is missing or null, return `near_t2t` or `insufficient`
and list the missing evidence in `blocking_for_strict_t2t`.
```

### `t2t_criteria/cases.yaml`（补充）

```yaml
- case_id: ont_r10_60x_near_t2t_fish
  context:
    platform: ont_r10
    coverage_x: 63
    genome_size_mb: 980
    target_level: near_t2t
    has_hic: false
  finishing_observed:
    telomere_coverage_pct: 0.71
    gap_count: 18
    busco_completeness: 0.943
  verdict: near_t2t
  confidence: 0.82
  key_reasoning: "端粒 71% < strict-T2T 要求，但满足 near-T2T 标准。18 个 gap 经人工确认均在着丝粒区域。"
  outcome: "human 采纳 adopt_near_t2t，downstream annotation 正常。"

- case_id: hifi_insect_strict_t2t_candidate
  context:
    platform: pacbio_hifi
    coverage_x: 45
    genome_size_mb: 380
    target_level: strict_t2t
    has_hic: true
  finishing_observed:
    telomere_coverage_pct: 0.93
    gap_count: 7
    hic_misassembly_count: 2
    busco_completeness: 0.985
  verdict: strict_t2t_candidate
  confidence: 0.88
  key_reasoning: "所有指标满足 strict-T2T 阈值。Hi-C 2 个 breakpoint 经检查为着丝粒区域，不影响判断。"
  outcome: "human 采纳 adopt_strict_t2t。"
```

## 测试

- `test_run_telomere_check_parses_tidk`：fake tidk TSV → 正确提取 motif 和 coverage_pct。
- `test_run_telomere_check_unavailable`：tidk command not found → notes 记录，不 fail，`telomere_detected=None`。
- `test_run_gap_survey`：fake seqkit 输出 → gap_count 和 total_gap_bases 正确。
- `test_generate_review_tasks_skill_context`：LLM mock 验证 prompt 包含两个 skill pack 的 SKILL.md。
- `test_generate_review_tasks_llm_fallback`：LLM 失败 → Python fallback 生成 `fallback_tasks`，不 fail。
- `test_generate_review_tasks_writes_two_artifacts`：同时写入 `AssemblyT2TReadiness` 和 `AssemblyReviewTasks` artifact。
- `test_generate_review_tasks_strict_t2t_constraint`：LLM mock 返回 `strict_t2t_candidate` 但 telomere_coverage_pct=0.5 → Python 层应检测并降级或记录 blocking_reason（验证安全边界有效）。
- `test_t2t_final_gate_strict_always_blocks`：target_level=strict_t2t → 任何 policy 下都 block_human。
- `test_t2t_final_gate_near_t2t_auto`：target=near_t2t + verdict=near_t2t + confidence=0.85 + auto policy → 不阻断，直接 draft_ready。
- `test_t2t_final_gate_near_t2t_auto_sets_draft_ref`：near-T2T auto 采纳时在 commit 前设置 `draft_assembly_ref`。
- `test_apply_t2t_final_review_sets_draft_ref`：resume adopt_near_t2t → `draft_assembly_ref` 正确。

## 验收

- `AssemblyT2TReadiness` 和 `AssemblyReviewTasks` artifact 均写入磁盘，tasks 字段非空（除 LLM 完全失败时用 fallback）。
- strict-T2T 路径在任何 policy 下必须进 human gate（测试验证）。
- near-T2T auto 路径不阻断，设置 `draft_assembly_ref` 后直接 commit。
- `apply_t2t_final_review` resume 后 `draft_assembly_ref` 正确设置，`commit_assembly_plan` 不报错。
- LLM prompt 包含两个 skill pack 的 `skill_text`，测试中可验证（mock 记录调用参数）。
