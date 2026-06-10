# 08-03 LongReadCandidateAgent

任务状态：待实施。

## 目标

根据 InputProfiler 确认的路线（cyclone / ont / hifi），运行对应候选组装工具，执行 QC，然后 LLM decision gate 给出下一步动作。三条路线共享同一套 `run_candidates → collect_candidates → evaluate_qc → decision_gate` 节点，区别只在 `freeze_candidates` 内部的分支。

HiFi 是与 Cyclone/ONT 并列的一级路线，不是 fallback。

## 前置

task 02 完成：`stage_outputs.assembly.status == "route_selected"`，`AssemblyRouteDecision` 可用。

## 三条路线的工具映射

| 路线 | 主工具 | baseline（同时运行） |
|---|---|---|
| cyclone | Flye `--nano-raw` | 无 |
| ont | hifiasm（`--ont`） | Flye `--nano-hq` |
| hifi | hifiasm（无 read-type flag） | 无 |

**关于 hifiasm HiFi 命令**：hifiasm 没有 `--hifi` flag。HiFi 模式是 hifiasm 的默认输入处理模式，命令直接传 reads 即可：
```bash
hifiasm -o <prefix> -t <threads> <hifi_reads>
```
ONT 模式需要显式 flag：
```bash
hifiasm --ont -o <prefix> -t <threads> <ont_reads>
```

## Descriptor 变更

### 新增 `descriptors/methods/assembly/hifiasm_ont_candidate.yaml`

```yaml
method_name: assembly.candidate.hifiasm_ont
stage: assembly
package_ref: hifiasm
goal: "hifiasm --ont 模式，ONT R10 reads，near-T2T primary candidate。"
required_inputs:
  - name: ont_reads
    type: clean_long_reads_or_fofn
expected_outputs:
  - name: primary_assembly
    type: draft_assembly
resource_hints:
  cpu_cores: 32
  memory_gb: 128.0
  walltime_hours: 72.0
```

### 升级 `descriptors/methods/assembly/hifiasm_candidate.yaml`

现有 descriptor goal 描述为 "compatibility route"，不可执行。改为 HiFi 一级路线：

```yaml
method_name: assembly.candidate.hifiasm
stage: assembly
package_ref: hifiasm
goal: "hifiasm 默认模式，PacBio HiFi reads，高精度 primary candidate。"
required_inputs:
  - name: hifi_reads
    type: clean_hifi_reads
expected_outputs:
  - name: primary_assembly
    type: draft_assembly
resource_hints:
  cpu_cores: 32
  memory_gb: 256.0
  walltime_hours: 48.0
```

### 更新 `descriptors/registry.yaml`

```yaml
methods:
  assembly.candidate.hifiasm_ont: assembly/hifiasm_ont_candidate.yaml  # 新增

by_stage:
  assembly:
    - assembly.candidate.hifiasm_ont  # 新增，排在 flye 前
    - assembly.candidate.flye
    - assembly.candidate.hifiasm
    - assembly.candidate.nextdenovo
    - assembly.candidate.canu
```

## 节点实现：`app/stages/assembly/nodes.py`

### 常量（替换旧的 `ASSEMBLY_AUTO_CANDIDATES` 等，task 06 删除旧常量）

```python
HIFIASM_ONT_METHOD  = "assembly.candidate.hifiasm_ont"
HIFIASM_HIFI_METHOD = "assembly.candidate.hifiasm"
FLYE_METHOD         = "assembly.candidate.flye"   # 已有，不变

ROUTE_TO_PRIMARY = {
    "cyclone": FLYE_METHOD,
    "ont":     HIFIASM_ONT_METHOD,
    "hifi":    HIFIASM_HIFI_METHOD,
}
ROUTE_TO_BASELINE = {
    "ont": FLYE_METHOD,
}
```

### `freeze_candidates`

根据 `selected_route` 决定 freeze 哪些 candidate。核心是私有辅助 `_freeze_one_candidate`（约 45 行），在里面用 `if/elif` 处理三种工具的 script 差异，不抽象基类：

```python
def _reads_ref_for_route(route, assembly_out):
    if route == "hifi":
        return assembly_out.get("hifi_reads_ref")
    if route in {"ont", "cyclone"}:
        if assembly_out.get("ont_reads_ref"):
            return assembly_out["ont_reads_ref"]
        primary_platform = assembly_out.get("primary_long_read_platform")
        if primary_platform in {"cyclone", "cycloneseq", "ont", "ont_r9", "ont_r10"}:
            return assembly_out.get("primary_long_reads_ref")
        return None
    return None

def _freeze_one_candidate(method_name, reads_ref, params, state):
    if method_name == FLYE_METHOD:
        # 复用现有 _flye_parameters, _flye_read_paths 辅助函数
        # script: flye {read_flag} {reads} --out-dir {dir} --genome-size {g} --threads {t}
        # cp assembly.fasta → draft_assembly.fasta
    elif method_name == HIFIASM_ONT_METHOD:
        # script: hifiasm --ont -o {prefix} -t {threads} {ont_reads}
        #         gfatools gfa2fa {prefix}.p_ctg.gfa > primary.fasta
    elif method_name == HIFIASM_HIFI_METHOD:
        # script: hifiasm -o {prefix} -t {threads} {hifi_reads}   # 无 --hifi flag
        #         gfatools gfa2fa {prefix}.p_ctg.gfa > primary.fasta
    return FrozenAttemptSpec(...)
```

`freeze_candidates` 必须按 route 选择 reads：`hifi` route 只使用 `hifi_reads_ref`；`ont` / `cyclone` route 使用 `ont_reads_ref` 或已确认兼容的 ONT/Cyclone long-read ref。不要盲目使用 `primary_long_reads_ref`，因为 InputProfiler 在同时存在 HiFi 和 ONT 时会把 primary 指向 HiFi，而显式 ONT route 仍应使用 ONT/Cyclone reads。

### `run_candidates` / `collect_candidates`

遍历 `frozen_candidates`，各自调 `run_frozen_attempt` 和验证输出文件，写 `AssemblyCandidateBundle`（现有 schema），注册 artifact，写 ledger。

一个 candidate 失败不整体 fail；全部失败才 `status=failed`。

### `evaluate_qc`

seqkit stats（直接 subprocess，轻量）+ 可选 BUSCO（复用 `assembly_review.eval.busco` descriptor）。

加载 `load_skill_context("qc_interpretation")`，但 `evaluate_qc` **不调 LLM**——它只运行工具、填 `AssemblyQCSummary` 字段，以及根据 `rules["tiers"]` 标记 `qc_passed`。LLM 的解读在下一个节点 `decision_gate`。

```python
def evaluate_qc(state):
    ctx = load_skill_context("qc_interpretation")
    tiers = ctx["rules"].get("tiers", {})
    # ... run seqkit, optionally BUSCO ...
    # qc_passed = n50_mb >= tiers["draft"]["n50_mb_min"] and ...
    # 写 AssemblyQCSummary，不做路由判断
```

### `decision_gate`

LLM 节点。加载 `load_skill_context("qc_interpretation")`，构建 prompt：

```
[System]
You are an expert assembly quality evaluator.
{ctx["skill_text"]}   ← SKILL.md 全文

[Rules]
{ctx["rules"]}        ← rules.yaml 序列化

[Cases]
{ctx["cases"]}        ← cases.yaml，最多 3 条最相关的

[Allowed actions]
{ctx["allowed_actions"]}  ← ["adopt_draft","enter_hic","t2t_finishing","block_human"]

[Tool outputs]
{qc_summaries}
{input_profile}
target_level: {target_level}
```

LLM 必须输出：`decision`（从 allowed_actions 选）、`selected_candidate_id`、`confidence`、`reasoning`、`uncertainty_notes`。

**Python 路由逻辑**（LLM 调用后执行，不再问 LLM）。安全边界和低置信度阻断必须先执行，任何低置信度 `adopt_draft` 都不能自动采纳：

```python
# 1. 安全边界优先于 LLM decision
if all_candidates_failed:
    return _block_for_human(state, "all_candidates_failed")
if all_candidates_fragmented:
    return _block_for_human(state, "all_candidates_fragmented")

# 2. 低置信度优先于所有决策分支
if confidence < 0.6 or decision == "block_human":
    → status = "blocked_human"

elif decision == "adopt_draft":
    → status = "draft_ready"

elif decision == "enter_hic":
    if has_hic:
        → status = "hic_evidence_required"
    else:
        # has_hic=False 时，不盲目进 T2T，根据 target_level 决定
        if target_level == "strict_t2t":
            → status = "t2t_finishing_required"
            # 并在 notes 中记录："enter_hic requested but no Hi-C data; proceeding to T2T finishing"
        elif target_level == "near_t2t":
            → status = "t2t_finishing_required"
        else:  # draft
            → status = "draft_ready"
            # notes: "enter_hic requested but target is draft and no Hi-C; adopting as draft"

elif decision == "t2t_finishing":
    → status = "t2t_finishing_required"
```

`safety_boundaries`（rules.yaml 中）的条件永远强制走 block_human，LLM decision 无效：

```python
# 检查 safety_boundaries，优先于 LLM decision
if all_candidates_failed:
    return _block_for_human(state, "all_candidates_failed")
if qc_summaries and all(candidate.n50_mb is not None and candidate.n50_mb < 0.1 for candidate in qc_summaries):
    return _block_for_human(state, "all_candidates_fragmented")
if selected_candidate and selected_candidate.n50_mb is not None and selected_candidate.n50_mb < 0.1:
    return _block_for_human(state, "selected_candidate_abnormally_fragmented")
```

不要把所有候选的 `min_n50_mb` 当作硬性安全边界；baseline 候选碎片化不能自动否决另一个健康候选。硬阻断只用于全部候选失败、全部候选碎片化，或 LLM/规则选中的候选本身碎片化异常。

### Gate 注册（`app/review/gates.py`）

```python
register_review_gate(ReviewGateDeclaration(
    stage="assembly",
    gate_id="candidate_decision_review",
    decisions_allowed=("adopt_draft", "enter_hic", "t2t_finishing", "request_data", "stop_human_review"),
    decision_applier="app.stages.assembly.nodes:apply_candidate_decision_review",
    prompt_bundle="assembly.qc_interpretation",
    prompt_adapter="generic",
    template_preferred_decision="adopt_draft",
))
```

## 现有代码的处理

`plan_candidate_attempts`（nodes.py:206）的 bundle 写入逻辑迁移到 `_write_candidate_bundle` 私有辅助，在 `collect_candidates` 中调用。`plan_candidate_attempts` 在 task 06 删除，本任务暂保留。

`freeze_flye_attempt` / `run_flye_attempt` / `collect_flye_artifacts` 在 task 06 通过路由绕过后删除，本任务暂保留。

## 测试

- `test_freeze_candidates_cyclone`：route=cyclone → 只 freeze flye，script 含 `--nano-raw`。
- `test_freeze_candidates_ont`：route=ont → freeze hifiasm_ont（含 `--ont`）+ flye baseline（含 `--nano-hq`）。
- `test_freeze_candidates_hifi`：route=hifi → 只 freeze hifiasm，script 不含 `--hifi` flag，不含 `--ont`。
- `test_freeze_candidates_route_specific_reads`：同时存在 HiFi 和 ONT reads 且 route=ont → hifiasm_ont / Flye 使用 `ont_reads_ref`，不使用指向 HiFi 的 `primary_long_reads_ref`。
- `test_collect_candidates_partial_failure`：一个失败 → 另一个仍收集；全部失败 → `status=failed`。
- `test_evaluate_qc_no_llm`：`evaluate_qc` 不调 LLM（mock 验证 LLM 调用次数为 0）。
- `test_evaluate_qc_busco_unavailable`：BUSCO 不可用 → `busco_completeness=None`，notes 记录，不 fail。
- `test_decision_gate_skill_context_injected`：LLM mock 验证 prompt 包含 `skill_text` 和 `cases`。
- `test_decision_gate_enter_hic_no_hic_draft`：decision=enter_hic + has_hic=False + target=draft → `draft_ready`。
- `test_decision_gate_enter_hic_no_hic_near_t2t`：decision=enter_hic + has_hic=False + target=near_t2t → `t2t_finishing_required`。
- `test_decision_gate_safety_boundary`：全部候选碎片化或选中候选 N50 < 100kb → block_human，无论 LLM decision 是什么。
- `test_decision_gate_low_confidence`：confidence=0.55 且 decision=adopt_draft → block_human。

## 验收

- 三条路线 fake binary 下各自跑通 freeze → run → collect → evaluate_qc → decision_gate。
- hifiasm HiFi 的 script **不含** `--hifi` 或 `--ont` flag。
- `load_skill_context("qc_interpretation")` 在 decision_gate 中被调用，LLM prompt 包含 SKILL.md 内容。
- safety_boundaries 硬阻断逻辑在任何 LLM decision 下均生效（测试验证）。
- `AssemblyQCSummary` artifact 写入磁盘。
