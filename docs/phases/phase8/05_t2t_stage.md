# 08-05 T2T Stage

任务状态：已实现。`t2t` 是 `hic_scaffolding` 后、`repeat_annotation` 前的顶层
stage，负责对当前 assembly 进行端粒末端检查，并在保守 ONT 证据满足时输出新的
patched FASTA。

## Input and routing

输入 assembly 依次取自 Hi-C 的 `final_assembly_for_downstream`（统一 contract）、
legacy fallback keys（`final_assembly_for_annotation`、
`chromosome_scaffolded_assembly_candidate_ref`），以及 assembly review 的 
`final_assembly_for_downstream`、legacy fallback keys（
`final_assembly_for_scaffolding_or_annotation`、`final_assembly_ref`）。

ONT reads 优先使用 `clean_long_reads`，缺失时回退 `raw_long_reads`。
没有 Hi-C 或 long reads 不会阻断 annotation：stage 写明 warning/skip reason 后
交接原 assembly。

**Implementation note (2026-06-28)**: 统一 contract key `final_assembly_for_downstream` 
已添加到所有 assembly-producing stages。T2T 的 `_assembly_ref()` 优先搜索统一 key，
然后 fallback 到 legacy keys 以保持向后兼容。

所有 assembly 都会路由到 `t2t`。只有 run config 或 CLI 将 `t2t` 放入
`local_exec` 时才执行 minimap2 以及 auto-patch；否则是可追溯的 candidate-only
handoff。

## Execution contract

Python 负责 FASTA/FASTQ 流式扫描、motif 端粒信号、PAF 解析、候选筛选和报告；
`minimap2 -x map-ont --secondary=no` 提供 ONT-to-assembly PAF。`tidk` 与
`samtools` 不是首版依赖。

默认 motif 为 `TTAGGG`，默认 `auto_patch`。自动 patch 要求：terminal-window
10 kb 内至少四个 motif copies、MAPQ ≥20、目标末端距离 ≤1 kb、有效 overlap
≥500 bp，以及至少一条支持 ONT read；如需更保守可把 `min_supporting_reads`
调回 2 或更高。选择 MAPQ/overlap 最优 read 的
未对齐 tail；tail 冲突、嵌合嫌疑和不足支持只输出候选，不修改 FASTA。

所有输出位于 `artifacts/t2t/<session>/t2t_finishing/`：端粒扫描、telomeric
reads、candidate 表、status summary、Markdown 报告；成功 patch 时还包括
`patched_assembly.fa`、patch records 和 patched scan。输入 assembly 永不覆盖，
下游 annotation 优先消费 `stage_outputs.t2t.final_assembly_for_annotation`。

## Validation

本地 `tests/test_t2t.py` 使用 fake minimap2 覆盖 scan → PAF → patch → rescan
和 annotation handoff。HPC `test_07_t2t.sh` 是不绑定永久项目数据的 smoke
placeholder；后续可用现场构造的小数据或真实项目数据填充 `T2T_ASSEMBLY` 与
`T2T_ONT_READS` 后做进一步验证。

**Test status (HPC 2026-06-26)**:
- 本地单元测试: 16 passed (test_t2t.py)
- HPC 测试: PASS (smoke placeholder without real fixture data)
- 所有测试涵盖 descriptor registry、run config、alignment、auto-patch、manual review gates
