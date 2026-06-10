# 08-01 Schema 扩展与 Knowledge Pack 基础设施

任务状态：已实施（Phase 8 Slice 1）。

实现说明：

- `AssemblyInputProfile` 已包含 dedicated `uncertainty_notes: list[str]`。
- `ASSEMBLY_CONTRACT_METHOD` 已删除，`app/` 中无残留引用。
- knowledge pack 采用 skill-like 结构：`rules.yaml` + `SKILL.md` + 可选 `cases.yaml`，不是 YAML-only pack。
- `estimated_coverage` 字段已在 schema 中保留；当前 profiler 尚未实际估算覆盖度，后续 runner/QC 切片可补齐。

## 目标

两件事：扩展 schema 承载新的数据类型和路线概念；建立 skill-like knowledge pack 的加载机制，让专家规则、专家推理方式和案例样本从文件注入节点，而不硬编码在代码里。

## Schema 变更

### `app/schemas/run_config.py`

**扩展 `AssemblyRunConfig`**：

```python
class AssemblyRunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flye_parameters: FlyeRunParameters | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"] = "draft"
    assembly_route: Literal["cyclone", "ont", "hifi", "auto"] = "auto"
```

`flye_parameters` 不动，现有测试继续通过。`assembly_route = "auto"` 时由 InputProfiler 推断；显式指定时跳过 LLM 推荐，直接走对应 route。

**扩展 `ReviewPolicyStage`**（`run_config.py:55`）：

```python
ReviewPolicyStage = Literal["assembly_review", "assembly", "hic_scaffolding"]
```

### `app/schemas/artifacts.py`

追加在文件末尾，不修改任何现有 class：

```python
ASSEMBLY_INPUT_PROFILE  = "assembly.input_profile"
ASSEMBLY_ROUTE_DECISION = "assembly.route_decision"
ASSEMBLY_QC_SUMMARY     = "assembly.qc_summary"
ASSEMBLY_T2T_READINESS  = "assembly.t2t_readiness"
ASSEMBLY_REVIEW_TASKS   = "assembly.review_tasks"

class AssemblyInputProfile(BaseModel):
    """InputProfiler 的输出：数据画像 + LLM 推荐路线。"""
    session_id: str
    # 通用字段（优先用这两个）
    primary_long_reads_ref: str | None = None
    primary_long_read_platform: str | None = None  # "cyclone", "ont_r9", "ont_r10", "pacbio_hifi"
    # 保留向后兼容
    ont_reads_ref: str | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    estimated_coverage: float | None = None
    estimated_genome_size: int | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"]
    has_hifi: bool = False
    has_hic: bool = False
    recommended_route: Literal["cyclone", "ont", "hifi", "insufficient_data"]
    recommendation_confidence: float
    recommendation_reasoning: str
    uncertainty_notes: list[str] = Field(default_factory=list)
    profile_source: Literal["manifest", "run_config", "run_config_explicit", "llm_inferred"]
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyRouteDecision(BaseModel):
    """用户或 LLM 最终确认的路线决策。"""
    session_id: str
    selected_route: Literal["cyclone", "ont", "hifi"]
    decided_by: Literal["llm_auto", "human_override", "run_config_explicit"]
    rationale: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyQCSummary(BaseModel):
    """单个候选组装的 QC 摘要。"""
    session_id: str
    candidate_id: str
    method_name: str
    n50_mb: float | None = None
    contig_count: int | None = None
    total_length_mb: float | None = None
    busco_completeness: float | None = None
    busco_lineage: str | None = None
    qv_score: float | None = None
    gap_count: int | None = None
    telomere_detected: bool | None = None
    qc_passed: bool
    qc_notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyT2TReadiness(BaseModel):
    """T2TFinishingAgent 的综合评估结果。"""
    session_id: str
    candidate_id: str
    t2t_verdict: Literal["adopt_draft", "near_t2t", "strict_t2t_candidate", "insufficient", "request_data"]
    llm_confidence: float
    llm_reasoning: str
    uncertainty_notes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    telomere_coverage_pct: float | None = None
    gap_count: int | None = None
    total_gap_bases: int | None = None
    hic_misassembly_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyReviewTaskItem(BaseModel):
    priority: Literal["high", "medium", "low"]
    category: str
    finding: str
    action: str
    can_auto_resolve: bool = False


class AssemblyReviewTasks(BaseModel):
    """T2TFinishingAgent 生成的人工 review 任务列表。"""
    session_id: str
    candidate_id: str
    t2t_status_estimate: Literal["near_t2t", "strict_t2t_candidate", "insufficient"]
    confidence: float
    tasks: list[AssemblyReviewTaskItem] = Field(default_factory=list)
    blocking_for_strict_t2t: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### `app/schemas/shared_parameters.py`

补充 hifiasm ONT 模式的实际 flag（原来是 `"not_preferred"`）：

```python
"ont_raw": { ..., "hifiasm": "ont" },
"ont_hq":  { ..., "hifiasm": "ont" },
```

新增 Cyclone 平台别名：

```python
PLATFORM_TO_SHARED_READ_TYPE = {
    "cycloneseq": "ont_raw",
    "cyclone":    "ont_raw",  # 新增
    "ont":        "ont_raw",
    "clr":        "pacbio_clr",
    "pacbio_hifi":"pacbio_hifi",
}
```

## Knowledge Pack 基础设施（Skill-Like 设计）

专家知识有三种形态，对应三类文件：

- **Rules YAML**：机器可读的阈值、安全边界、允许动作集合。作为 guardrail，LLM 不能突破。
- **SKILL.md**：自然语言的专家推理方式、异常情况处理、权衡逻辑。作为 reasoning scaffold，让 LLM 推理有领域上下文而不只是做数字比较。
- **cases.yaml**（可选）：过去决策的结构化案例样本。作为 few-shot，帮助 LLM 在相似情境下迁移判断。

### 目录结构

```
descriptors/
  knowledge_packs/
    assembly/
      input_profiler/
        rules.yaml
        SKILL.md
      route_selector/
        rules.yaml
        SKILL.md
        cases.yaml
      qc_interpretation/
        rules.yaml
        SKILL.md
        cases.yaml
      t2t_criteria/
        rules.yaml
        SKILL.md
        cases.yaml
      review_task_guide/
        rules.yaml
        SKILL.md
```

### 加载机制：`app/stages/assembly/knowledge.py`

```python
"""Assembly skill context loader."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml

# repo-root 相对路径，不依赖 cwd
_PACK_ROOT = Path(__file__).parent.parent.parent.parent / "descriptors" / "knowledge_packs" / "assembly"


def load_skill_context(pack_name: str) -> dict[str, Any]:
    """Load rules, skill text, and cases for a named pack.

    Returns a dict with keys: rules, skill_text, cases, allowed_actions.
    Any missing component returns an empty/None value — callers must handle gracefully.
    """
    pack_dir = _PACK_ROOT / pack_name
    if not pack_dir.exists():
        return {"rules": {}, "skill_text": None, "cases": [], "allowed_actions": []}

    rules: dict[str, Any] = {}
    rules_path = pack_dir / "rules.yaml"
    if rules_path.exists():
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}

    skill_text: str | None = None
    skill_path = pack_dir / "SKILL.md"
    if skill_path.exists():
        skill_text = skill_path.read_text(encoding="utf-8")

    cases: list[dict[str, Any]] = []
    cases_path = pack_dir / "cases.yaml"
    if cases_path.exists():
        cases = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or []

    allowed_actions: list[str] = rules.get("allowed_actions", [])

    return {
        "rules": rules,
        "skill_text": skill_text,
        "cases": cases,
        "allowed_actions": allowed_actions,
    }
```

Path 解析方式：`Path(__file__)` 从 `knowledge.py` 自身位置推算 repo root，不依赖进程工作目录。

### LLM 节点调用约定

LLM 节点构建 prompt 时，始终把 skill context 的三个部分都注入：

```python
ctx = load_skill_context("qc_interpretation")
# rules 作为结构化约束传入
# skill_text 作为 system prompt 的领域背景
# cases 作为 few-shot examples
# allowed_actions 作为输出约束（LLM 只能从中选择 decision）
```

LLM **不能**：
- 选择 `allowed_actions` 之外的动作
- 建议运行 rules 中未列出的工具
- 直接修改 genome 文件

LLM **必须**输出的字段：`decision`（从 allowed_actions 中选）、`confidence`、`reasoning`、`uncertainty_notes`（不确定的点）。

### 第一版 Pack 内容示例：`qc_interpretation/rules.yaml`

```yaml
pack_id: assembly.qc_interpretation
version: "1.0"

allowed_actions:
  - adopt_draft
  - enter_hic
  - t2t_finishing
  - block_human

tiers:
  draft:
    n50_mb_min: 1
    contig_count_max: 5000
    busco_completeness_min: 0.85
  near_t2t:
    n50_mb_min: 10
    contig_count_max: 500
    busco_completeness_min: 0.92
  strict_t2t:
    n50_mb_min: 30
    contig_count_max: 100
    busco_completeness_min: 0.98
    telomere_coverage_pct_min: 0.80
    gap_count_max: 50

safety_boundaries:
  always_block_human:
    - "all_candidates_failed"
    - "all_candidates_fragmented"
    - "selected_candidate_abnormally_fragmented"
  never_auto_adopt_strict_t2t: true

data_sufficiency:
  min_coverage_for_assembly: 20
  min_coverage_for_near_t2t: 40
  min_coverage_for_strict_t2t: 60
```

### 第一版 Pack 内容示例：`qc_interpretation/SKILL.md`

```markdown
# Assembly QC Interpretation Skill

## Expert Reasoning Procedure

When evaluating an assembly candidate, do NOT mechanically apply thresholds.
Follow this sequence:

1. **Context-first**: Check coverage and platform before reading QC numbers.
   A 30x R9 assembly with N50=5Mb is different from a 30x R10 assembly with
   the same N50. The R10 one likely has more room to improve with more data;
   the R9 one may already be near its ceiling.

2. **Soft vs hard failures**: Low BUSCO (< 85%) is a soft failure if coverage
   is also low — the assembly may be incomplete, not wrong. Hard failure is
   when N50 drops below 500kb with 60x coverage — something went wrong in
   assembly, not just data quantity.

3. **Candidate comparison matters**: If hifiasm_ont and flye disagree on N50
   by more than 2x, that is a signal, not just noise. Report it explicitly
   and recommend human inspection rather than silently picking the better one.

4. **Missing signals are information**: If BUSCO could not run, say so and
   adjust confidence downward. Do not present a QC verdict as if all signals
   were available.

## Common Exceptions

- **Polyploid species**: Expected heterozygosity inflates contig count.
  If the genome survey indicated polyploidy, do not penalize high contig
  count as if it were a haploid assembly failure.
- **Large repeat-rich genomes (> 5 Gb)**: N50 thresholds scale with genome
  size. A 3 Mb N50 in a 6 Gb genome is proportionally better than in a 1 Gb
  genome.
- **Known difficult regions**: rDNA arrays and centromeres routinely produce
  gaps and low-complexity contigs. Their presence does not indicate overall
  assembly failure.

## Uncertainty Protocol

When confidence is below 0.7, always set `uncertainty_notes` to explain
what additional information would resolve the uncertainty. Prefer
`block_human` over a low-confidence automatic decision.
```

### 第一版 `qc_interpretation/cases.yaml`

```yaml
- case_id: ont_r10_fish_60x_near_t2t
  context:
    platform: ont_r10
    coverage_x: 63
    genome_size_mb: 980
    target_level: near_t2t
  qc_observed:
    n50_mb: 14.2
    contig_count: 312
    busco_completeness: 0.943
  decision: t2t_finishing
  reasoning: "N50 和 BUSCO 均满足 near-T2T 阈值，进入 T2T finishing 验证端粒和 gap。"
  outcome: "最终 adopt_near_t2t，gap 18 个均在着丝粒区域。"

- case_id: cyclone_insect_30x_draft
  context:
    platform: cyclone
    coverage_x: 31
    genome_size_mb: 420
    target_level: draft
  qc_observed:
    n50_mb: 2.1
    contig_count: 1840
    busco_completeness: 0.891
  decision: adopt_draft
  reasoning: "Cyclone 平台精度限制 + 覆盖度偏低，draft 阈值满足，直接采纳。"
  outcome: "assembly_review 通过，downstream annotation 正常运行。"
```

## 删除旧占位常量

`ASSEMBLY_CONTRACT_METHOD = "assembly.candidate_plan"`（`nodes.py:38`）在 registry 中无对应 descriptor，无调用方，本任务删除。

## 测试

- `test_load_skill_context_full`：pack 存在且有三个文件 → 返回 rules/skill_text/cases/allowed_actions。
- `test_load_skill_context_partial`：只有 rules.yaml → skill_text=None, cases=[]，不报错。
- `test_load_skill_context_missing`：pack 不存在 → 返回空 dict，不抛异常。
- `test_load_skill_context_path`：路径从 `knowledge.py` 位置推算，不依赖 cwd（在 /tmp 目录下运行测试也能找到文件）。
- `test_assembly_run_config_new_fields`：新字段有默认值，旧 JSON 不报错。
- `test_assembly_input_profile_profile_source`：`profile_source` 接受 `"run_config_explicit"` 和 `"llm_inferred"` 均合法。

## 验收

- `knowledge.py` 可独立导入，不依赖任何 stage 状态，不依赖 cwd。
- `descriptors/knowledge_packs/assembly/` 下五个 pack 目录存在，每个至少有 `rules.yaml` 和 `SKILL.md`。
- `load_skill_context` 返回 dict 包含 `allowed_actions`，LLM 节点用这个列表约束输出。
- 新 schema 类型在 `artifacts.py` 末尾，无现有 class 被修改。
- `ASSEMBLY_CONTRACT_METHOD` 删除后 grep 无残留。

已验证：`tests.test_phase8_slice1` 覆盖 schema、knowledge loader、path independence、partial/missing pack 行为。
