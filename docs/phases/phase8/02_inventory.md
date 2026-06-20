# 08-02 Inventory

任务状态：待校正实施。此文档统一使用 `inventory`：它负责把用户输入、manifest、asset registry、run-config 和已有 stage outputs 整理成后续 workflow 可消费的结构化画像。

## 目标

增强现有 `inventory` stage，使它不仅登记资产，还能明确后续 stage 的可用性和初始 hints：

- 数据类型：Cyclone/ONT/HiFi/Hi-C/short DNA/RNA/Iso-Seq/existing assembly。
- 目标层级：draft/chromosome/near_t2t/strict_t2t。
- assembly route hint：cyclone/ont/hifi/auto。
- stage availability：哪些 stage 应该运行，哪些应该跳过或等待数据。

`inventory` 不运行 assembly 方法，不做 Hi-C scaffolding，不做 T2T 判断。

## Current Implementation Compatibility

Commit `ca685a9` implemented a first route-profile slice inside `assembly`:

- `resolve_extended_inputs`
- `infer_input_profile`
- `select_route`
- `apply_route_decision`
- `assembly/route_selection` review gate

This does not conflict with the corrected design. It is a temporary assembly-local prelude that gathers exactly the data assembly needs for route selection. The broader workflow input profile still belongs in `inventory`.

Migration should be incremental:

1. Keep the existing assembly route-selection tests passing.
2. Add `inventory` data-profile fields without renaming `inventory`.
3. Let assembly read inventory hints when present, falling back to its current `resolve_extended_inputs` behavior.
4. Only after that, decide whether the assembly-local `input_profile` artifact remains as a route-selection snapshot or is replaced by inventory-owned profile refs.

## Boundary

`inventory` 负责：

- 解析 manifest / input paths / run-config。
- 生成 asset registry。
- 识别 existing assembly 入口。
- 汇总数据画像和 stage availability。
- 把显式用户意图保留为 hints。

`inventory` 不负责：

- 选择最终 assembly 方法。
- freeze/run/collect Flye 或 hifiasm。
- 生成 Hi-C scaffolding candidate。
- 评价 telomere/gap/T2T readiness。
- 自动降级用户的 target level。

## Suggested Output

在 `stage_outputs.inventory` 中补充：

```python
{
    "status": "completed",
    "entry_mode": "raw_reads | existing_assembly",
    "target_level": "draft | chromosome | near_t2t | strict_t2t",
    "data_profile": {
        "has_ont": bool,
        "has_cyclone": bool,
        "has_hifi": bool,
        "has_hic": bool,
        "has_short_dna": bool,
        "has_rnaseq": bool,
        "has_isoseq": bool,
        "has_existing_assembly": bool,
        "primary_long_read_platform": "cyclone | ont | pacbio_hifi | unknown",
        "estimated_genome_size": int | None,
        "estimated_coverage": float | None,
    },
    "assembly_hints": {
        "route": "cyclone | ont | hifi | auto",
        "method": "flye | hifiasm_ont | hifiasm_hifi | nextdenovo | canu | None",
        "reason": "...",
    },
    "stage_availability": {
        "read_qc": bool,
        "genome_survey": bool,
        "assembly": bool,
        "assembly_review": bool,
        "hic_scaffolding": bool,
        "t2t": bool,
        "repeat_annotation": bool,
        "gene_annotation": bool,
        "function_annotation": bool,
    },
    "skip_reasons": {
        "hic_scaffolding": "hic_reads_missing | target_not_chromosome | None",
        "t2t": "target_not_t2t | assembly_missing | None",
    },
}
```

These are routing hints. Each later stage must still validate required inputs before running.

## Target Level Rules

- `draft`: run through assembly/assembly_review; skip Hi-C and T2T unless explicitly requested later.
- `chromosome`: require Hi-C for `hic_scaffolding`; if Hi-C missing, block/request data rather than silently adopting draft.
- `near_t2t`: T2T stage should run after assembly review and optional Hi-C.
- `strict_t2t`: T2T stage must run and must block for human final confirmation.

If a user provides only an existing assembly, `inventory` should route directly to `assembly_review`, preserving downstream availability for Hi-C/T2T/repeat/gene/function annotation.

## Assembly Hints

Route hints stay coarse:

| Data profile | Route hint | Method hint |
|---|---|---|
| Cyclone / lower-quality ONT | `cyclone` | `flye` |
| ONT R10 / simplex long reads | `ont` | `hifiasm_ont` |
| PacBio HiFi | `hifi` | `hifiasm_hifi` |
| Missing or ambiguous long-read profile | `auto` | `None` |

These hints are not binding. The `assembly` stage can override or block if inputs are incompatible.

## Relationship To Run Config

Explicit run-config values should be copied into inventory output as user intent:

- `assembly.target_level`
- `assembly.assembly_route`
- `assembly.assembly_method`
- `assembly.hifi_reads_ref`
- `assembly.hic_reads_ref`

Do not turn `hic_reads_ref` into a decision to run Hi-C inside assembly. It only makes `hic_scaffolding` available.

## Tests

- `test_inventory_records_target_level`
- `test_inventory_sets_hic_scaffolding_available_when_hic_reads_present`
- `test_inventory_chromosome_without_hic_records_skip_or_block_reason`
- `test_inventory_near_t2t_marks_t2t_available`
- `test_inventory_existing_assembly_keeps_downstream_stage_availability`
- `test_inventory_does_not_emit_alternate_profile_stage_name`

## Acceptance

- All docs and tests call this stage `inventory`.
- `inventory` produces profile/hints, not execution decisions.
- Hi-C availability points to `hic_scaffolding`.
- T2T availability points to new `t2t` stage.
