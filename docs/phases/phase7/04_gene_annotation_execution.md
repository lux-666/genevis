# 07-04 基因注释执行契约

任务状态：本地实现已提交（commit `fcf48ec`）；真实 HPC smoke 待 task 08 执行。

## 目标

`gene_annotation` 不再只是规划阶段。当前任务的目标是把可执行的基因预测方法接入 annotation agent 主链，并形成稳定的候选基因模型契约，供 candidate review、EVM merge、BUSCO 和 function annotation 消费。

本任务关注“候选产生”：

```text
repeat_annotation / mask_review
  -> gene method planning review
  -> executable gene methods
  -> CandidateBundle
  -> candidate_evidence_review
```

最终基因集合并、BUSCO 和最终发布属于 task 05。

## 当前可执行边界

已落地的可执行方法：

- `gene_annotation.candidate.miniprot`
- `gene_annotation.candidate.augustus`

仍为 planning-only 的方法：

- `braker3`
- `maker`
- `gemoma`
- RNA-seq helper methods such as `hisat2`, `stringtie`, `isoquant`

执行模式统一为：

```text
freeze_<method>_attempt
  -> run_<method>_attempt
  -> collect_<method>_artifacts
```

freeze 节点生成 `FrozenAttemptSpec` 和 shell script，run 节点调用 `LocalRunner`，collect 节点只接受真实落盘的输出文件并注册 artifact。

## CandidateBundle 契约

每个成功执行的候选方法必须物化为一个候选 bundle。bundle 的职责是把“方法运行结果”与“后续合并所需的证据类型”分开。

必须包含：

- `method_name`: 完整方法名，例如 `gene_annotation.candidate.miniprot`
- `annotation_gff_ref`: 真实存在的候选 GFF3
- `gene_count`: 从 GFF3 解析出的 gene 数量
- `evidence_class`: EVM 使用的 evidence class，例如 `OTHER_PREDICTION`、`ABINITIO_PREDICTION` 或 `PROTEIN`
- `evm_source_type`: EVM weights 中的 source/type，例如 `augustus` 或 `miniprot`
- `evm_route`: EVM 命令参数路由，例如 `gene_predictions`、`protein_alignments` 或 `transcript_alignments`
- `proteins_fasta_ref`: 可选，若该候选已经物化蛋白序列
- `transcripts_fasta_ref`: 可选，若该候选已经物化转录本序列

禁止把 planning 阶段的 expected output 占位路径当作候选结果发布。collect 阶段必须以本地文件存在为准。

## 输入契约

`gene_annotation` 的基因组输入来自上游 annotation chain：

- 优先使用 `repeat_annotation.softmasked_genome_ref`
- 若 `mask_review` 选择 `skip`，回退为 assembly final ref
- 若 manifest 直接提供既有 annotation，走 provided annotation 路径

蛋白同源证据输入来自 manifest 中的 reference protein asset，供 Miniprot 使用。

AUGUSTUS 需要 `augustus_species_model`。该值属于 inventory/hints 通道，不属于普通 method parameter。若 method review 选择了 AUGUSTUS 但 hints 中没有 species model，review/readiness 必须阻断或要求操作者改选方法，不能让运行期才失败。

## Miniprot 契约

Miniprot 使用参考蛋白对屏蔽基因组进行同源注释。

命令形态：

```bash
miniprot --gff <genome.fa> <reference_proteins.fa> > <candidate.gff3>
```

collect 阶段必须：

- 注册 `gene_annotation.candidate_gff`
- 写入 `candidate_refs[method_name]`
- 写入 `candidate_gene_counts[method_name]`
- 生成 CandidateBundle
- 标记该 method 已执行

Miniprot 是 protein-homology-derived candidate，但 `miniprot --gff` 原生输出是 mRNA/CDS 风格的 gene-model-like GFF3，不是 EVM `--protein_alignments` 所需的 protein alignment feature 格式。

当前执行契约：

- `native_gff_route = gene_predictions`
- `evidence_class = OTHER_PREDICTION`
- `evm_source_type = miniprot`
- `protein_alignment_route.enabled = false`
- `protein_alignment_route.requires_converter = miniprot_GFF_2_EVM_alignment_GFF3`

只有在实现并验证 Miniprot GFF 到 EVM protein alignment GFF3 的转换器后，Miniprot 才能作为 `PROTEIN` / `--protein_alignments` 输入 EVM。

## AUGUSTUS 契约

AUGUSTUS 使用显式 species model 在屏蔽基因组上执行 ab initio prediction。

命令形态：

```bash
augustus --species=<augustus_species_model> --gff3=on <genome.fa> > <candidate.gff3>
```

readiness 规则：

- 缺 `augustus_species_model` 时，在 method planning/review 阶段阻断。
- species model 来自 inventory/hints，不能只通过 planner required parameters 填入。
- 若操作者确认使用 distant model，应在 warnings 中记录风险。

AUGUSTUS 输出按 EVM 语义归为 gene prediction evidence。

## 候选 Review 契约

`candidate_evidence_review` 消费 CandidateBundle 列表，而不是裸 GFF 路径。

review payload 应展示：

- 每个候选的方法名
- gene count
- evidence class/source
- artifact path
- warnings
- 未执行或被剔除方法的原因

review 决策写入 accepted candidate 集合，供 task 05 的 merge 阶段使用。

## 测试重构设计

gene annotation 测试应从“任务切片文件”重排为契约分层：

- `test_gene_annotation_methods.py`: Miniprot/AUGUSTUS freeze-run-collect、失败清理、artifact 注册。
- `test_gene_annotation_candidates.py`: CandidateBundle schema、gene count、evidence class/source/route、禁止占位路径泄漏。
- `test_gene_annotation_reviews.py`: method readiness、candidate review accept/reject、route trace。
- `test_gene_annotation_e2e.py`: fake tools 下 repeat -> gene candidate -> review 的最小闭环。

现有 `tests/test_gene_annotation_miniprot.py`、`tests/test_gene_annotation.py` 中的历史 slice 测试可逐步迁移到上述分层；迁移时保留行为覆盖，不保留旧任务命名。

## 验收

本任务完成时必须满足：

- Miniprot 和 AUGUSTUS 均可通过 freeze-run-collect 真实产出候选 GFF3。
- AUGUSTUS 缺 species model 时在 readiness/review 阶段阻断。
- 每个成功候选都有 CandidateBundle，且 bundle 引用的文件真实存在。
- Miniprot native GFF 的 EVM contract 是 `OTHER_PREDICTION` / `gene_predictions`，不得进入 `PROTEIN` / `protein_alignments`。
- `candidate_evidence_review` 只消费真实候选结果。
- 没有 planning expected output 占位路径进入 candidate bundle 或后续 merge 输入。
- 新增和迁移后的单元测试通过。
- task 08 能用 fake/local smoke 和 HPC smoke 验证该契约。
