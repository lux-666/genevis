# 07-05 候选合并、最终基因集与 BUSCO

任务状态：本地实现已提交（commit `fcf48ec`）；真实 HPC smoke 待 task 08 执行。

## 目标

本任务负责把 task 04 产出的候选基因模型提升为最终 gene set，并给最终 gene set 生成 BUSCO 质量信号。它是 `gene_annotation` stage 内部的后半段，不另立 stage。

链路位置：

```text
candidate_evidence_review
  -> EVM 或 single-candidate passthrough
  -> materialize final proteins/transcripts
  -> BUSCO on final materialized sequences
  -> final_gene_set_review
  -> commit_gene_annotation
```

核心原则：最终发布和 BUSCO 都必须读取同一套 final gene set resolver，不能让 planning 占位路径进入最终 bundle。

## 合并契约

输入来自 `candidate_evidence_review` 接受的 CandidateBundle。

### 单候选直通

当 accepted candidate 数量为 1 时，不运行 EVM：

- `merge_method = "single_candidate_passthrough"`
- `final_gff_ref` 指向该候选 GFF3
- 如果候选已有 proteins/transcripts，优先沿用并注册为 final refs
- 如果候选只有 GFF3，尝试用 gffread 物化 proteins/transcripts

### 多候选 EVM

当 accepted candidate 数量大于 1 时运行 EVM：

- gene prediction evidence 写入 `--gene_predictions`
- protein alignment evidence 写入 `--protein_alignments`
- transcript alignment evidence 写入 `--transcript_alignments`

EVM weights 文件必须使用真实 EVM 语义：

```text
EVIDENCE_CLASS<TAB>SOURCE_OR_TYPE<TAB>WEIGHT
```

禁止把文件路径写入 weights 文件。文件路径只属于 EVM 命令参数。

Miniprot 需要按输出格式区分：

- Miniprot native `--gff` 输出：`OTHER_PREDICTION` / `--gene_predictions`。
- Miniprot converted alignment GFF3：只有在实现并验证 `miniprot_GFF_2_EVM_alignment_GFF3` 后，才允许 `PROTEIN` / `--protein_alignments`。

AUGUSTUS 归为 `ABINITIO_PREDICTION` / `--gene_predictions`。GeneWise 等真正的 protein alignment GFF3 才能进入 `PROTEIN` / `--protein_alignments`。

## Final Gene Set Resolver

最终基因集由一个统一 resolver 发布，优先级如下：

1. `final_gff_ref`
2. `final_proteins_ref`
3. `final_transcripts_ref`
4. provided annotation refs

`commit_gene_annotation`、`freeze_gene_busco_attempt` 和 function annotation handoff 必须使用同一套最终 refs。

禁止行为：

- 从 `expected_outputs.predicted_proteins` 推断最终蛋白。
- 在文件不存在时仍把路径传给 BUSCO 或 function annotation。
- 在 bundle 中发布未物化的 `work/.../proteins.fa` 占位路径。

## gffread 物化契约

当最终 GFF3 可用而 proteins/transcripts 不可用时，使用 gffread 物化序列：

```bash
gffread <final.gff3> -g <genome.fa> -y <proteins.fa> -x <transcripts.fa>
```

物化产物必须满足：

- 文件真实存在且非空。
- 注册为 gene annotation artifact。
- 写入 `final_proteins_ref` / `final_transcripts_ref`。
- 被 `commit_gene_annotation` 发布为 `proteins_fasta_ref` / `transcripts_fasta_ref`。

若 gffread 缺失或失败，错误应落在 gene annotation 的执行/收集路径中；不能用旧 expected output 占位继续向下游冒充成功。

CandidateBundle 可以没有 `proteins_fasta_ref`；Final gene set 不可以在进入 function annotation 前缺少真实存在且非空的 `proteins_fasta_ref`。若 `requested_stages` 包含 `function_annotation`，`commit_gene_annotation` 必须发布真实 proteins FASTA，否则应 terminal fail 或停在 review/修复路径。

## BUSCO 契约

BUSCO 使用最终物化序列，而不是 planning expected outputs。

选择顺序：

1. 若 `final_proteins_ref` 存在，使用 `--mode proteins`。
2. 否则若 `final_transcripts_ref` 存在，使用 `--mode transcriptome`。
3. 两者都不存在时，记录 `busco_completeness_status = "missing"`，不冻结 BUSCO attempt。

BUSCO 失败不中止流程，但必须降级为明确的 missing/failed signal，并进入 `final_gene_set_review`。这保证 review 能看到“没有可靠 BUSCO”的事实，而不是流程假装完整。

BUSCO lineage 来自 lineage/species hints 或 run-config 中的显式 lineage。缺 lineage 时同样记录缺失信号，不运行 BUSCO。

## Final Gene Set Review

`final_gene_set_review` payload 必须包含：

- `final_gff_ref`
- `final_proteins_ref`
- `final_transcripts_ref`
- `merge_method`
- accepted candidate summary
- BUSCO completeness or missing reason
- warnings

review 决策接受后，`commit_gene_annotation` 发布最终 gene annotation bundle。

## Function Annotation Handoff

function annotation 的蛋白输入必须来自 gene annotation 最终 bundle：

- 首选 `proteins_fasta_ref`
- 兼容 `predicted_proteins_ref`
- 兼容旧 alias `predicted_proteins`

task 08 的 run-config 不应预猜 gene stage 的工作目录路径。推荐在 database provenance 中省略 `input_proteins_ref`，由 function annotation 在 provenance validation 时采用实际 resolved proteins ref，并在 bundle 中回填。

## 测试重构设计

本任务的测试应集中覆盖最终契约：

- `test_gene_annotation_merge.py`: EVM weights 三列格式、evidence class 路由、单候选 passthrough。
- `test_gene_annotation_materialization.py`: gffread 物化 proteins/transcripts、空文件失败、artifact 注册。
- `test_gene_annotation_busco.py`: BUSCO 使用 `final_proteins_ref`、transcript fallback、缺文件不运行 BUSCO、run failure 降级。
- `test_gene_annotation_final_bundle.py`: commit 只发布真实 final refs，禁止 expected output 占位。

历史测试可以继续存在，但断言应从“脚本包含某个字符串”升级为“生成的命令和文件符合生信语义”。

## 验收

本任务完成时必须满足：

- 单候选和多候选路径都能产出 `final_gff_ref`。
- 多候选 EVM weights 为 `EVIDENCE_CLASS<TAB>SOURCE_OR_TYPE<TAB>WEIGHT`。
- Miniprot native GFF 进入 `OTHER_PREDICTION` / `--gene_predictions`；只有 converter 产出的 Miniprot alignment GFF 才能进入 `PROTEIN` / `--protein_alignments`。
- AUGUSTUS 等 prediction evidence 进入 `--gene_predictions`；GeneWise 等真正 protein alignment evidence 进入 `--protein_alignments`。
- `final_proteins_ref` / `final_transcripts_ref` 来自真实物化文件。
- 若 `requested_stages` 包含 `function_annotation`，final bundle 必须发布真实存在且非空的 `proteins_fasta_ref`。
- BUSCO 使用 final refs，缺 final sequence 时干净降级为 missing。
- `gene_annotation_bundle.json` 不包含不存在的 expected output 占位路径。
- function annotation handoff 能解析到真实 proteins fasta。
