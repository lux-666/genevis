# 08-06 子图连线与旧代码清理

任务状态：待实施。

## 目标

把 task 01-05 实现的所有节点连接成可运行的 assembly 子图；清理旧节点；补充端到端集成测试；确认所有新 gate 均在 registry 中注册并可验证。

## 前置

task 01-05 全部完成，所有节点函数均在 `nodes.py` 中实现且单元测试通过。

## 确认图定义位置

```bash
grep -rn "add_node\|add_edge\|enter_assembly\|commit_assembly" app/ 2>/dev/null | grep -v ".pyc" | head -30
```

在找到的位置更新节点连接。若 `app/stages/gene_annotation/builder.py` 存在，仿照它新建 `app/stages/assembly/builder.py`（约 100 行）；否则在现有图定义文件中更新。

## 完整节点连接

```python
# InputProfiler
graph.add_node("enter_assembly",          enter_assembly)
graph.add_node("resolve_extended_inputs", resolve_extended_inputs)
graph.add_node("infer_input_profile",     infer_input_profile)
graph.add_node("select_route",            select_route)

# LongReadCandidateAgent
graph.add_node("freeze_candidates",  freeze_candidates)
graph.add_node("run_candidates",     run_candidates)
graph.add_node("collect_candidates", collect_candidates)
graph.add_node("evaluate_qc",        evaluate_qc)
graph.add_node("decision_gate",      decision_gate)

# HiCEvidenceAgent
graph.add_node("freeze_hic_alignment",  freeze_hic_alignment)
graph.add_node("run_hic_alignment",     run_hic_alignment)
graph.add_node("collect_hic_alignment", collect_hic_alignment)
graph.add_node("evaluate_hic_evidence", evaluate_hic_evidence)
graph.add_node("hic_decision_gate",     hic_decision_gate)

# T2TFinishingAgent
graph.add_node("run_telomere_check",    run_telomere_check)
graph.add_node("run_gap_survey",        run_gap_survey)
graph.add_node("generate_review_tasks", generate_review_tasks)
graph.add_node("t2t_final_gate",        t2t_final_gate)

# 出口（现有节点，不动）
graph.add_node("commit_assembly_plan",     commit_assembly_plan)
graph.add_node("fail_assembly",            fail_assembly)
graph.add_node("block_assembly_for_human", block_assembly_for_human)
```

## 路由函数（新增，放在 `nodes.py` 末尾）

```python
def route_after_resolve(state):
    return "fail" if _assembly_output(state).get("status") == "failed" else "infer_profile"

def route_after_infer_profile(state):
    return "select_route"  # profile 节点不 fail，LLM 失败时写 notes 降级

def route_after_select_route(state):
    s = _assembly_output(state).get("status")
    if s == "blocked_human":
        return "block"
    return "freeze_candidates"

def route_after_collect_candidates(state):
    return "fail" if _assembly_output(state).get("status") == "failed" else "evaluate_qc"

def route_after_decision_gate(state):
    s = _assembly_output(state).get("status")
    return {
        "draft_ready":           "commit",
        "hic_evidence_required": "freeze_hic_alignment",
        "t2t_finishing_required":"run_telomere_check",
        "blocked_human":         "block",
        "failed":                "fail",
    }.get(s, "fail")

def route_after_hic_gate(state):
    s = _assembly_output(state).get("status")
    return {
        "draft_ready":           "commit",
        "t2t_finishing_required":"run_telomere_check",
        "blocked_human":         "block",
        "failed":                "fail",
    }.get(s, "fail")

def route_after_t2t_gate(state):
    sup = state.get("supervision_state")
    if sup == "blocked_human":
        return "end"
    if sup == "failed_terminal":
        return "fail"
    return "commit"
```

## 旧代码清理

### 删除（grep 验证无调用后执行）

| 函数/常量 | 位置 | 替代 |
|---|---|---|
| `resolve_assembly_inputs` | nodes.py:69 | `resolve_extended_inputs` |
| `build_assembly_candidate_set` | nodes.py:100 | `freeze_candidates` 内部逻辑 |
| `_planned_candidate_methods` | nodes.py:143 | 无，删除 |
| `select_primary_and_fallback` | nodes.py:153 | `select_route` |
| `plan_candidate_attempts` | nodes.py:206 | `freeze_candidates` + `_write_candidate_bundle` |
| `freeze_flye_attempt` | nodes.py:291 | `freeze_candidates` 内 Flye 分支 |
| `run_flye_attempt` | nodes.py:379 | `run_candidates` |
| `collect_flye_artifacts` | nodes.py:411 | `collect_candidates` |
| `ASSEMBLY_AUTO_CANDIDATES` | nodes.py:27 | `ROUTE_TO_PRIMARY` |
| `ASSEMBLY_COMPATIBILITY_CANDIDATES` | nodes.py:32 | 无，删除 |

验证命令：
```bash
grep -rn "resolve_assembly_inputs\|build_assembly_candidate_set\|select_primary_and_fallback\|plan_candidate_attempts\|freeze_flye_attempt\|run_flye_attempt\|collect_flye_artifacts\|ASSEMBLY_AUTO_CANDIDATES\|ASSEMBLY_COMPATIBILITY_CANDIDATES\|_planned_candidate_methods" app/ tests/ 2>/dev/null
```

### 保留不动的辅助函数

```
_assembly_output, _update_assembly_output, _fail_update, _block_for_human,
_flye_read_paths, _flye_parameters, _read_qc_assembly_input_ref,
_asset_input_ref, _genome_size_prior, _read_type_prior, _shared_parameters,
_long_read_platform, _genome_survey_result_ref, _unique_refs,
_validate_methods, _candidate_bundle_ref
```

`FLYE_READ_TYPE_FLAGS`、`FLYE_REQUIRED_PARAMS`、`GENOME_SIZE_REQUIRED` 保留（`freeze_candidates` Flye 分支使用）。

### 测试清理

删除引用已删除节点的旧测试（grep 确认）：

- `test_resolve_assembly_inputs_*` → 已被 task 02 的 `test_resolve_extended_inputs_*` 替代
- `test_build_assembly_candidate_set` → 删除
- `test_select_primary_and_fallback` → 删除
- `test_plan_candidate_attempts` → 删除
- `test_freeze_flye_attempt` → 删除
- `test_run_flye_attempt` → 删除
- `test_collect_flye_artifacts` → 删除
- `test_assembly_rerun_*`（7 个已知失败的 Flye rerun 测试）→ 删除

## Gate Registry 验收测试

新增 `tests/test_assembly_gate_registry.py`，验证所有 Phase 8 新增 gate 均已注册且行为正确：

```python
def test_all_assembly_gates_registered():
    from app.review.gates import get_review_gate
    get_review_gate("assembly", "route_selection")           # task 02
    get_review_gate("assembly", "candidate_decision_review") # task 03
    get_review_gate("assembly", "hic_misassembly_review")    # task 04
    get_review_gate("assembly", "t2t_final_review")          # task 05

def test_unknown_assembly_gate_rejected():
    from app.review.gates import get_review_gate
    with pytest.raises(KeyError):
        get_review_gate("assembly", "nonexistent_gate")
```

## 集成测试

新增 `tests/test_assembly_agent_integration.py`：

- `test_assembly_agent_cyclone_draft`：cyclone route → `draft_ready` → commit，验证 `commit_assembly_plan` 的 committed_keys 完整。
- `test_assembly_agent_ont_near_t2t_auto`：ont route + auto policy → decision_gate `adopt_draft`，不阻断。
- `test_assembly_agent_hifi_t2t_finishing_human`：hifi route → `t2t_finishing` → human gate（strict-T2T）。
- `test_assembly_agent_ont_with_hic`：ont + has_hic=True → HiCEvidenceAgent → `t2t_finishing_required`（near-T2T target）。
- `test_assembly_agent_ont_with_hic_draft_target`：ont + has_hic=True + target=draft → HiCEvidenceAgent → `draft_ready`（不进 T2T）。
- `test_assembly_agent_enter_hic_no_hic_near_t2t`：decision=enter_hic + has_hic=False + target=near_t2t → `t2t_finishing_required`（不 block）。
- `test_assembly_agent_block_on_low_confidence`：LLM confidence < 0.5 → block_human。

## 行数守卫

```bash
wc -l app/stages/assembly/nodes.py
```

当前约 836 行，删除 ~200 行旧节点，新增 ~300 行新节点，目标 < 1000 行。超过 1100 行需重新审查。

## 文档更新

- `docs/stage_contracts.md` assembly 部分替换为新路由图
- `docs/phases/phase8/README.md` status 改为 `已完成`
- `docs/phases/phase_completion_plan.md` 更新 phase 8 状态

## 验收

- 四个新 assembly gate 均在 registry 中注册，`test_assembly_gate_registry.py` 通过。
- fake binaries 下，三条路线（cyclone/ont/hifi）各自能从 `enter_assembly` 跑到 `commit_assembly_plan`。
- grep 验证已删除函数无残留引用。
- `nodes.py` 行数 < 1100。
- `assembly_review` 相关测试不受影响（committed_keys 不变）。
- `test_assembly_agent_ont_with_hic_draft_target` 通过：draft 目标 + 好的 Hi-C 不强制进 T2T。
- `test_assembly_agent_enter_hic_no_hic_near_t2t` 通过：无 Hi-C 数据时根据 target_level 路由，不盲目进 T2T。
