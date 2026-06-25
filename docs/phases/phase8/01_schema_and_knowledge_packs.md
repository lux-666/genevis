# 08-01 Schema And Knowledge Packs

任务状态：重新校正。此任务不再为 assembly 内部的输入画像或 Hi-C 子代理创建 schema，而是补齐既有 stage 的 contract：

- `inventory`：输入画像和 route/method hints。
- `assembly`：long-read assembly method provenance。
- `hic_scaffolding`：chromosome-level scaffold/curation candidate provenance。
- `t2t`：新增 top-level stage 的 telomere scans、extension candidates、patch records 与 handoff artifacts。

## 目标

用 schema 明确 stage 边界，避免把方法、数据类型和阶段混成一个大 Assembly Agent。

## Stage Boundary

| Concern | Stage | Schema 方向 |
|---|---|---|
| 输入识别、资产登记、profile | `inventory` | `InputProfile` / inventory output fields |
| long-read assembly 方法选择和执行 | `assembly` | candidate bundle + method provenance |
| assembly 评估和人工选择 | `assembly_review` | 保持现有 review contract |
| Hi-C scaffolding / manual curation | `hic_scaffolding` | scaffold candidate / workspace / decision |
| telomere/T2T finishing | `t2t` | scans / candidates / conservative patch records |

## Run Config Delta

`AssemblyRunConfig` 可以继续承载 assembly route 和 method hints，但不再把 Hi-C 或 T2T 行为塞进 assembly route。

Current implementation note from commit `ca685a9`:

- `AssemblyRunConfig` already has `hifi_reads_ref`, `hic_reads_ref`, `target_level`, and `assembly_route`.
- `target_level` currently allows only `draft | near_t2t | strict_t2t`; adding `chromosome` remains an explicit delta.
- There is not yet an `assembly_method` field; adding it should be backward-compatible and optional.

```python
AssemblyRoute = Literal["cyclone", "ont", "hifi", "auto"]
AssemblyMethod = Literal[
    "flye",
    "hifiasm_ont",
    "hifiasm_hifi",
    "nextdenovo",
    "canu",
]

class AssemblyRunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flye_parameters: FlyeRunParameters | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    target_level: Literal["draft", "chromosome", "near_t2t", "strict_t2t"] = "draft"
    assembly_route: AssemblyRoute = "auto"
    assembly_method: AssemblyMethod | None = None
```

`hic_reads_ref` 可以继续作为 shortcut hint，但实际 Hi-C 执行属于 `hic_scaffolding` stage。

## Inventory Profile

`inventory` 应输出可被后续 stage 消费的数据画像。可以先以 `stage_outputs.inventory` 字段落地，不必急着新增独立 Pydantic model。

Commit `ca685a9` already added an `AssemblyInputProfile` model and writes `assembly.input_profile` artifacts from `assembly`. Treat that as a compatibility artifact for the existing route-selection slice, not as proof that input profiling is a separate top-level stage. The future `inventory` profile can either:

- reuse compatible fields and write them under `stage_outputs.inventory`, or
- keep the existing assembly artifact as a route-selection snapshot while `inventory` owns broader data availability.

建议字段：

```python
{
    "entry_mode": "raw_reads | existing_assembly",
    "target_level": "draft | chromosome | near_t2t | strict_t2t",
    "data_profile": {
        "has_ont": bool,
        "has_cyclone": bool,
        "has_hifi": bool,
        "has_hic": bool,
        "has_short_reads": bool,
        "has_rnaseq": bool,
        "has_isoseq": bool,
        "primary_long_read_platform": "cyclone | ont | pacbio_hifi | unknown",
        "estimated_genome_size": int | None,
        "estimated_coverage": float | None,
    },
    "assembly_hints": {
        "route": "cyclone | ont | hifi | auto",
        "method": "flye | hifiasm_ont | hifiasm_hifi | nextdenovo | canu | None",
    },
    "stage_availability": {
        "assembly": bool,
        "assembly_review": bool,
        "hic_scaffolding": bool,
        "t2t": bool,
    },
}
```

这些字段是 hint，不是执行决定。`assembly` 仍需要验证输入和方法兼容性。

## Assembly Method Provenance

Assembly candidate bundle 需要记录方法来源，方便后续 review / hic / t2t 追踪：

```python
{
    "method_name": "assembly.candidate.flye",
    "assembly_route": "cyclone",
    "assembly_method": "flye",
    "input_ref": "...",
    "input_type": "clean_long_reads | long_reads.fofn | hifi_reads",
    "expected_outputs": {"draft_assembly_fasta": "..."},
    "metadata": {
        "method_application_subgraph": "assembly.method.flye",
        "read_correction_method": "none",
        "read_correction_ref": None,
        "draft_assembly_ref_status": "planned_path_not_materialized | materialized",
    },
}
```

Flye、hifiasm-ONT、hifiasm-HiFi 等方法可以共享 candidate schema，但由 method-specific subgraph 负责 freeze/run/collect。

## Hi-C Scaffolding Contracts

Hi-C schema 属于现有 `hic_scaffolding` stage。Phase 8 可以增强命名和 provenance，但不创建 assembly 内部 Hi-C agent。

建议补充 artifacts：

```python
HIC_SCAFFOLDING_WORKSPACE = "hic_scaffolding.workspace"
HIC_SCAFFOLDING_CANDIDATE = "hic_scaffolding.candidate"
HIC_SCAFFOLDING_DECISION = "hic_scaffolding.decision"
HIC_SCAFFOLDING_RECHECK_SUMMARY = "hic_scaffolding.recheck_summary"
```

输出仍写入 `stage_outputs.hic_scaffolding`，并提供：

```python
{
    "pre_hic_assembly_ref": "...",
    "chromosome_scaffolded_assembly_candidate_ref": "...",
    "final_assembly_for_t2t_or_annotation": "...",
    "selected_candidate_provenance": {...},
}
```

## T2T Artifacts

新增 `t2t` stage 需要自己的 artifact constants 和 models：

Commit `ca685a9` already added assembly-scoped prototypes:

```python
ASSEMBLY_T2T_READINESS = "assembly.t2t_readiness"
ASSEMBLY_REVIEW_TASKS = "assembly.review_tasks"
AssemblyT2TReadiness
AssemblyReviewTasks
```

Do not delete them during the stage-boundary cleanup. They should be treated as legacy/prototype contracts until the new `t2t` stage has its own artifacts and migration tests.

```python
T2T_TELOMERE_MOTIF_SUMMARY = "t2t.telomere_motif_summary"
T2T_TERMINAL_TELOMERE_CHECK = "t2t.terminal_telomere_check"
T2T_GAP_SURVEY = "t2t.gap_survey"
T2T_READINESS = "t2t.readiness"
T2T_REVIEW_TASKS = "t2t.review_tasks"
T2T_FINAL_DECISION = "t2t.final_decision"
```

Minimum model shapes:

```python
class T2TReadiness(BaseModel):
    session_id: str
    input_assembly_ref: str
    target_level: Literal["near_t2t", "strict_t2t"]
    verdict: Literal["not_t2t", "near_t2t", "strict_t2t", "insufficient"]
    confidence: float
    reasoning: str
    telomere_summary_ref: str | None = None
    gap_survey_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class T2TReviewTasks(BaseModel):
    session_id: str
    input_assembly_ref: str
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

## Knowledge Packs

Keep knowledge packs aligned with real consumers:

Current implementation note: `ca685a9` added packs under `descriptors/knowledge_packs/assembly/`, including `input_profiler`, `route_selector`, `qc_interpretation`, `t2t_criteria`, and `review_task_guide`. These names are acceptable as existing compatibility packs, but new work should avoid treating `input_profiler` or `t2t_criteria` as stage names.

```text
descriptors/knowledge_packs/
  inventory/
    input_profile/
  assembly/
    route_selector/
    method_selection/
  hic_scaffolding/
    curation_review/
  t2t/
    criteria/
    review_task_guide/
```

Do not create unused packs for HERRO, polishing, telomere completion, or Verkko until a node consumes them.

## Tests

- `test_inventory_profile_records_data_availability`
- `test_inventory_profile_uses_inventory_stage_name`
- `test_assembly_candidate_bundle_records_method_application_provenance`
- `test_hic_scaffolding_contract_remains_existing_stage`
- `test_t2t_artifact_schemas_validate`
- `test_t2t_stage_name_is_registered_once`

## Acceptance

- No schema or pack invents a second input-profile stage.
- Hi-C artifacts are under `hic_scaffolding`, not `assembly.hic_curation`.
- T2T artifacts are under a new `t2t` stage.
- Assembly method provenance supports more than Flye without changing downstream contracts.
- Existing `assembly.*` Slice 1 artifacts remain readable during migration.
