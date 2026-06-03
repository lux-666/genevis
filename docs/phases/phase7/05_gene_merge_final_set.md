# 07-05 候选基因模型合并与最终基因集

任务状态：规划中；依赖 task 04（候选基因模型执行切片）和 task 03（merge review gate）。

## 目标

将一个或多个候选基因注释方法产出的 GFF3 文件通过 EVM 合并为最终基因集，经 BUSCO 完整性验证后送入最终基因集 review gate。EVM 权重在合并阶段决定，候选-证据 review（task 03）确认候选值得合并，合并步骤完成整合，最终基因集 review 以 BUSCO 为信号接受或拒绝。

## 当前状态（已验证）

- `gene_annotation` stage 无任何 EVM/MAKER 集成；`commit_gene_annotation` 目前直接把规划阶段的期望输出路径写入 bundle，不执行合并。
- `GeneAnnotationBundle`（`app/schemas/artifacts.py`）已有 `genes_gff3_ref` / `predicted_proteins_ref` 字段，可直接承载最终基因集路径。
- BUSCO 在 `assembly_review` 已有完整模式：`freeze_busco_attempts` → `run_busco_attempts` → `collect_busco_artifacts`，以及 `assembly_review.eval.busco` 在 `config/envs.yaml` 的 conda env 条目；复用该集成模式，不另立新机制。
- `ArtifactMeta.from_file(...)` 是注册制品的统一入口，模式已稳定。
- `lineage_or_species_hints` 已由 `resolve_gene_annotation_inputs` 从 inventory 中读取 `busco_lineage`，可直接复用。

## 位置（chain position）

```
task 04：运行基因注释方法
    ↓ candidate_gff_refs（来自 merge review gate 决策）
task 05：EVM 合并（本任务） → 运行 BUSCO（proteins/transcriptome 模式）
    ↓ final_gff_ref + BUSCO 完整性信号
    → final_gene_set_review gate（ReviewPayload）
task 06：功能注释
```

## 范围

- 在 `gene_annotation` commit 路径中增加合并执行节点（不另立 stage）。
- EVM 合并执行器：接收候选 GFF3 列表 + 权重表（来自 merge review gate），产出合并后的最终基因集 GFF3 作为持久制品。
- 单候选直通：若仅有一个 `candidate_gff_ref`，跳过 EVM，将其直接提升为最终基因集，记录 `merge_method: single_candidate_passthrough`。
- BUSCO 执行器：以 `--mode proteins` 或 `--mode transcriptome` 在最终基因集上运行 BUSCO，将完整性分数作为信号写入 `final_gene_set_review` ReviewPayload。
- 将合并与 BUSCO 节点接入 `gene_annotation` 的 node graph，位置在候选-证据 review gate 之后。
- 在 `config/envs.yaml` 中补充 `gene_annotation.evm` 和 `gene_annotation.busco` 方法条目。

## 非目标

- 产出候选基因模型（task 04）。
- 功能注释（task 06）。
- EVM 权重的人工审核界面（属于 task 03 的 merge review gate）。
- 在本任务中实现 MAKER 合并路径（EVM 为主，MAKER 不在本任务范围）。

## 已决事项

- **合并主干**：EVM 为主。考虑到 EVM 维护风险，设有单候选直通兜底：若只有一个候选 GFF3，跳过 EVM，直接将其作为最终基因集，记录 `merge_method: single_candidate_passthrough`。
- **合并位置**：在 `gene_annotation` commit 路径中实现（不另立 stage），与 task 03 决策一致。
- **BUSCO lineage**：从 `lineage_or_species_hints["busco_lineage"]` 读取（`resolve_gene_annotation_inputs` 已从 inventory 填充此字段）；若不存在则沿用 `assembly_review` BUSCO 使用的相同 lineage（通过 shared_parameters 约定）；两者均不存在时记录缺失信号、不中止流程。
- **BUSCO 模式**：优先 `--mode proteins`；若无蛋白序列则退回 `--mode transcriptome`；两者均不可用时记录为缺失信号。
- **制品键名**：`stage_output["final_gff_ref"]`，artifact_type 为 `gene_annotation.final_gff`。

## 任务

- [ ] 在 `gene_annotation/nodes.py` 中添加 `freeze_evm_attempt` 节点：输入为 merge review gate 决策中的 `candidate_gff_refs` 列表和权重表，生成 EVM pipeline 命令脚本，写入工作目录，冻结为 attempt payload（模式与 `freeze_busco_attempts` 保持一致）。
- [ ] 实现单候选直通逻辑：`freeze_evm_attempt` 中检测 `candidate_gff_refs` 数量；若为 1，跳过脚本生成，在 attempt payload 中设 `merge_method: single_candidate_passthrough`，直接记录源路径。
- [ ] 添加 `run_evm_attempt` 节点：使用 `LocalRunner` 执行冻结的 EVM 脚本，捕获 stdout/stderr，错误时在 stage_output 中记录失败原因（不抛异常）。
- [ ] 添加 `collect_evm_artifacts` 节点：将合并后的 GFF3 注册为 `ArtifactMeta`（artifact_type `gene_annotation.final_gff`），将路径写入 `stage_output["final_gff_ref"]`，记录 `merge_method`（`evm` 或 `single_candidate_passthrough`）。
- [ ] 添加 `freeze_gene_busco_attempt` 节点：从 `stage_output["final_gff_ref"]` 推断蛋白/转录本序列路径，按已决事项的模式选择策略，生成 BUSCO 命令脚本，冻结为 attempt payload；lineage 来自 `lineage_or_species_hints["busco_lineage"]`。
- [ ] 添加 `run_gene_busco_attempt` 节点：使用 `LocalRunner` 执行冻结的 BUSCO 脚本，捕获结果；错误时降级为缺失信号，不中止。
- [ ] 添加 `collect_gene_busco_artifacts` 节点：解析 BUSCO short summary，提取完整性分数（Complete/Single/Duplicated/Fragmented/Missing 百分比），注册为 `ArtifactMeta`（artifact_type `gene_annotation.busco_summary`），将指标写入 `stage_output["busco_completeness"]`。
- [ ] 构建 `final_gene_set_review` ReviewPayload：将 `busco_completeness` 作为信号字段写入，`candidate_id` 设为 session_id，通过 gate registry 触发 review gate（与 task 03 的 merge review gate 对称）。
- [ ] 将上述节点接入 `gene_annotation` node graph：在候选-证据 review gate accept 分支之后，依次连接 `freeze_evm_attempt` → `run_evm_attempt` → `collect_evm_artifacts` → `freeze_gene_busco_attempt` → `run_gene_busco_attempt` → `collect_gene_busco_artifacts` → `final_gene_set_review` gate。
- [ ] 在 `config/envs.yaml` 中添加 `gene_annotation.evm` 和 `gene_annotation.busco` 方法条目，`required_tools` 分别指定 `EVidenceModeler` 和 `busco`，conda_env 均指向 `env_geno_exec`（或按实际 env 调整）。
- [ ] 添加单元测试：`freeze_evm_attempt` 生成正确的权重表命令；单候选直通时不生成 EVM 脚本；`busco_completeness` 信号出现在 `final_gene_set_review` ReviewPayload 中；`final_gff_ref` 在合并成功后出现在 `stage_output` 中。
- [ ] task 04 首个执行切片落地后，以真实候选文件执行本地冒烟验证（smoke test），确认 EVM 命令可运行并产出 GFF3。

## 验收

本任务完成时：

- 若有多个候选 GFF3，`stage_output["merge_method"]` 为 `evm`，`final_gff_ref` 指向 EVM 产出的合并 GFF3；
- 若只有一个候选 GFF3，`stage_output["merge_method"]` 为 `single_candidate_passthrough`，EVM 脚本未被生成或执行；
- BUSCO 完整性分数（Complete/Single/Duplicated/Fragmented/Missing）出现在 `final_gene_set_review` 的 `ReviewPayload.signals` 中；
- `final_gff_ref` 和 `busco_completeness` 均出现在 `gene_annotation` stage_output 中，且已通过 `ArtifactMeta.from_file` 注册到制品账本；
- `config/envs.yaml` 中存在 `gene_annotation.evm` 和 `gene_annotation.busco` 方法条目；
- 新增单元测试全部通过，现有 `gene_annotation` 测试无退化。
