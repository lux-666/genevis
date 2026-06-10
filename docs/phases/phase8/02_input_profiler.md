# 08-02 InputProfiler

任务状态：已实施（Phase 8 Slice 1）。

实现说明：

- `resolve_extended_inputs` 已替换旧 `resolve_assembly_inputs`，并保留旧 Flye path 需要的 `input_ref` / `input_type` 等兼容字段。
- `infer_input_profile` 写入 `AssemblyInputProfile` artifact；显式 `assembly_route` 会跳过 LLM 并直接写入 `AssemblyRouteDecision`。
- route selector LLM 失败时返回 `recommended_route="insufficient_data"`、`confidence=0.0`，阻断到人工/补数据 review；不使用启发式自动 fallback。
- `select_route` 默认使用 strict policy；自动采纳只在显式 `review_policy.mode="auto"` 且 `confidence >= 0.7` 时发生。
- `route_selector_prompt` / `route_selector_source` 不写入 graph state。

## 目标

InputProfiler 负责两件事：从 manifest / asset registry / stage_outputs / run_config 收集所有可用数据线索，产出结构化的 `AssemblyInputProfile`；用 LLM + `route_selector` skill context 推荐最佳组装路线，把推荐和理由暴露给 TUI 和后续 decision gate。

## 前置

task 01 完成：`AssemblyInputProfile`、`AssemblyRouteDecision` schema 可用；`load_skill_context` 可调用；`route_selector/rules.yaml` 和 `route_selector/SKILL.md` 存在。

## 节点函数（替换 `resolve_assembly_inputs`）

### `resolve_extended_inputs`

纯数据收集，无 LLM，替换现有 `resolve_assembly_inputs`（nodes.py:69）。

**ONT/Cyclone reads**（按优先级）：
1. `read_qc` stage output 中 `assembly_input_ref` / `filtered_long_reads_fofn_ref` / `long_reads_fofn_ref`（现有 `_read_qc_assembly_input_ref` 逻辑保留）
2. asset registry 中 `asset_type in {"clean_long_reads", "raw_long_reads"}` 的条目

**HiFi reads**：
1. `run_config.assembly.hifi_reads_ref`（显式指定）
2. asset registry 中 `asset_type == "pacbio_hifi"` 的条目

**Hi-C reads**：
1. `run_config.assembly.hic_reads_ref`（显式指定）
2. asset registry 中 `asset_type in {"hic_reads", "hi_c_reads"}` 的条目

**平台信息**（按优先级）：
1. `shared_parameters.platform`
2. `read_qc.platform`
3. asset 的 `platform` 字段

输出到 `stage_outputs.assembly`（保留所有现有字段以兼容 Flye 节点）：

```python
{
    "status": "resolved_extended",
    # 通用字段（新）
    "primary_long_reads_ref": ...,       # ONT/Cyclone/HiFi 中优先级最高的 long reads
    "primary_long_read_platform": ...,   # 推断出的平台字符串
    # 具体数据线索
    "ont_reads_ref": ...,
    "hifi_reads_ref": ...,
    "hic_reads_ref": ...,
    "platform": ...,
    "has_hifi": bool,
    "has_hic": bool,
    "target_level": ...,
    "genome_size_prior": ...,
    "explicit_route": "cyclone" | "ont" | "hifi" | None,
    # 保留兼容现有 Flye 节点
    "input_ref": ...,
    "input_type": ...,
    "input_source": ...,
    "genome_survey_result_ref": ...,
    "shared_parameters": ...,
    "read_type_prior": ...,
}
```

`primary_long_reads_ref` 的优先级：HiFi > ONT/Cyclone（当 `has_hifi=True` 时 HiFi 优先，因为 HiFi 精度更高）。

无 long reads 时 → `_fail_update(state, "assembly requires long reads")`。

### `infer_input_profile`

LLM 节点，接在 `resolve_extended_inputs` 之后。

**若 `explicit_route` 非 None**（用户在 run_config 显式指定了路线）：
- 跳过 LLM
- `profile_source = "run_config_explicit"`，`recommended_route = explicit_route`，`confidence = 1.0`
- 直接写 `AssemblyInputProfile` 和 `AssemblyRouteDecision`，`decided_by = "run_config_explicit"`
- 状态 → `profile_ready`

**否则**（auto 模式）：
- 加载 `load_skill_context("route_selector")`
- LLM 接收：
  - 数据画像（reads refs 存否、平台、coverage 估算、target_level）
  - `ctx["rules"]`（routes 定义、selection_priority）
  - `ctx["skill_text"]`（SKILL.md，专家推理流程）
  - `ctx["cases"]`（过去决策样本）
  - `ctx["allowed_actions"]`（LLM 只能从中选 `recommended_route`）
- LLM 输出：`recommended_route`、`confidence`、`reasoning`、`uncertainty_notes`
- confidence < 0.5 → `recommended_route = "insufficient_data"`，只写 `AssemblyInputProfile`，不写 `AssemblyRouteDecision`，路由到 block_human / data review
- 写 `AssemblyInputProfile` artifact（`data/artifacts/assembly/<session>/input_profile.json`）
- **不写 `AssemblyRouteDecision`**：路线确认在 `select_route` 节点
- 状态 → `profile_ready`

### `select_route`

TUI 接入点：推荐是建议，`select_route` 是确认。

**auto policy 且 confidence ≥ 0.7**：
- 直接采纳推荐
- 若 `recommended_route == "insufficient_data"`，不得采纳；写 `active_interrupt`，阻断到人工/补数据 review
- 写 `AssemblyRouteDecision`，`decided_by = "llm_auto"`
- 状态 → `route_selected`

**strict policy 或 confidence < 0.7**：
- 写 `active_interrupt`，等人工确认
- TUI 展示：推荐路线 + reasoning + uncertainty_notes + 可选路线列表（只显示数据满足条件的）
- resume 后调 `apply_route_decision`

`apply_route_decision`（约 20 行）：读 resume payload 中的 gate decision，先把 `select_cyclone` / `select_ont` / `select_hifi` 映射为 route 字面量 `cyclone` / `ont` / `hifi`，再验证 route 在可用路线内。`stop_human_review` 走人工终止；`insufficient_data` 不允许生成 `AssemblyRouteDecision`，必须继续 block_human / data review。验证通过后写 `AssemblyRouteDecision`，`decided_by = "human_override"`，状态 → `route_selected`。

```python
ROUTE_DECISION_MAP = {
    "select_cyclone": "cyclone",
    "select_ont": "ont",
    "select_hifi": "hifi",
}
selected_route = ROUTE_DECISION_MAP.get(resume_payload["decision"])
if selected_route is None:
    return _block_for_human(state, "route_selection_requires_valid_route")
if selected_route not in {"cyclone", "ont", "hifi"}:
    return _block_for_human(state, "route_selection_requires_valid_route")
```

### Gate 注册（`app/review/gates.py`）

```python
register_review_gate(ReviewGateDeclaration(
    stage="assembly",
    gate_id="route_selection",
    decisions_allowed=("select_cyclone", "select_ont", "select_hifi", "stop_human_review"),
    decision_applier="app.stages.assembly.nodes:apply_route_decision",
    prompt_bundle="assembly.route_selector",
    prompt_adapter="generic",
    template_preferred_decision="select_ont",
))
```

## `route_selector` Knowledge Pack 内容

### `route_selector/rules.yaml`

```yaml
pack_id: assembly.route_selector
version: "1.0"

allowed_actions:
  - cyclone
  - ont
  - hifi
  - insufficient_data

routes:
  cyclone:
    description: "BGI CycloneSeq 平台，类 ONT R9，Flye nano-raw 作主路线"
    primary_tool: flye
    primary_tool_flag: nano-raw
    note: "第一版启发式规则，基于 CycloneSeq 与 ONT R9 的相似性。如有更多实测数据，请更新此 pack。"

  ont:
    description: "ONT 平台，R10+ 高质量 reads，hifiasm --ont 作主路线"
    primary_tool: hifiasm_ont
    baseline_tool: flye
    note: "R10.4.1+ 覆盖度 ≥ 40x 时 hifiasm(ONT) 可达 near-T2T。R9 数据质量受限，建议降级到 cyclone route 或 block_human。"

  hifi:
    description: "PacBio HiFi，hifiasm 默认模式（无特殊 read-type flag）"
    primary_tool: hifiasm_hifi
    note: "HiFi 数据存在时优先考虑此 route。"

selection_priority_notes:
  - "有 HiFi reads（覆盖度 ≥ 30x）→ hifi route"
  - "有 ONT R10 reads，覆盖度 ≥ 40x → ont route"
  - "有 CycloneSeq 或 ONT R9 → cyclone route（第一版启发式，可被 cases 覆盖）"
  - "数据不足（< 20x）→ insufficient_data"
```

### `route_selector/SKILL.md`

```markdown
# Route Selection Skill

## Expert Reasoning Procedure

Route selection is primarily a data-quality question, not a tool-preference
question. Follow this order:

1. **Data availability first**: Does the user have HiFi reads with ≥ 30x
   coverage? If yes, hifi route almost always wins — HiFi assembly accuracy
   is fundamentally higher. Do not override this for ONT reads unless the
   user has an explicit reason (cost, availability).

2. **Platform matters for ONT**: R10.4.1+ with Q20 median is a different
   data type than R9/CycloneSeq with Q12. The cyclone route rule for
   CycloneSeq is a first-version heuristic based on similarity to ONT R9.
   If you have actual CycloneSeq QC data showing Q20+ median, treat it as
   ONT R10 instead. Update the cases file when you have evidence.

3. **Coverage floors are soft**: The 40x floor for near-T2T is a typical
   minimum, not a hard cutoff. A 35x R10 dataset from a small (< 500 Mb)
   genome may be fine. A 50x R9 dataset for a 3 Gb genome may not be enough.
   Adjust based on genome size and platform quality.

4. **Uncertainty is information**: If the platform cannot be determined,
   say so with `uncertainty_notes` and set confidence low. The TUI will
   surface this to the user. Do not guess silently.

## When to Choose `insufficient_data`

- Coverage < 15x regardless of platform.
- Platform completely unknown and no QC stats available.
- The only available data is a genome survey result with no reads refs.
```

## 现有函数的处理

`resolve_assembly_inputs`（nodes.py:69）被 `resolve_extended_inputs` 完全替换，删除。以下辅助函数**保留不动**（`resolve_extended_inputs` 继续调用）：

`_read_qc_assembly_input_ref`, `_asset_input_ref`, `_genome_size_prior`, `_read_type_prior`, `_shared_parameters`, `_long_read_platform`, `_genome_survey_result_ref`

`build_assembly_candidate_set`（nodes.py:100）和 `select_primary_and_fallback`（nodes.py:153）在 task 06 删除，本任务不动。

## 测试

- `test_resolve_extended_inputs_hifi_priority`：同时有 ONT 和 HiFi reads → `primary_long_reads_ref` 指向 HiFi，`has_hifi=True`。
- `test_resolve_extended_inputs_no_reads`：无任何 reads → `status=failed`。
- `test_resolve_extended_inputs_preserves_input_ref`：`input_ref` 字段在输出中仍存在（兼容 Flye 节点）。
- `test_infer_input_profile_explicit_route`：`run_config.assembly_route="hifi"` → `profile_source="run_config_explicit"`，跳过 LLM。
- `test_infer_input_profile_llm_with_skill_context`：LLM mock 收到 `skill_text` 非 None；返回有效 `recommended_route`。
- `test_infer_input_profile_low_confidence`：confidence < 0.5 → `recommended_route="insufficient_data"`，block_human。
- `test_select_route_defaults_to_strict_and_blocks_high_confidence`：无 review policy 时使用系统 strict 默认，即使 confidence ≥ 0.7 也进入 route selection human gate。
- `test_select_route_auto_policy_can_adopt_high_confidence`：显式 auto policy + confidence ≥ 0.7 → 直接写 `AssemblyRouteDecision`，不阻断。
- `test_select_route_insufficient_data_blocks`：即使 policy=auto，也不为 `recommended_route="insufficient_data"` 写 `AssemblyRouteDecision`。
- `test_select_route_strict_policy`：strict policy → block_human，等 resume。
- `test_apply_route_decision_maps_gate_decision`：`select_ont` → `selected_route="ont"`。
- `test_apply_route_decision_invalid`：resume payload 中无法映射为有效 route 的 decision → 被拒绝。
- `test_infer_input_profile_llm_failure_returns_insufficient_data`：LLM 不可用时低置信阻断，不自动 fallback。

## 验收

- `AssemblyInputProfile` artifact 写入磁盘，包含 `recommended_route`、`reasoning`、`uncertainty_notes`。
- `select_route` 在显式 auto policy + high confidence 下不阻断流程；默认 strict policy 会阻断等待人工确认。
- 现有 Flye 路径（`input_ref` 字段）在 `resolve_extended_inputs` 输出中保留，`freeze_flye_attempt` 无需改动。
- 删除 `resolve_assembly_inputs` 后 grep 无残留引用。
- LLM prompt 包含 `skill_text`，测试中可验证（mock 记录调用参数）。
- `recommended_route == "insufficient_data"` 时不生成 `AssemblyRouteDecision`。
