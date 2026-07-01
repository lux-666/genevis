# 07-06 功能注释执行契约

任务状态：DIAMOND Slice 1 本地实现已落地；bundle 已停止发布未物化的 GO/KEGG/Pfam 占位 refs；DIAMOND 已写入 `FunctionMethodResult`；真实 HPC smoke 待 task 08 执行。

## 目标

`function_annotation` 消费 gene annotation 的最终 proteins FASTA，执行一个或多个功能注释方法，并发布真实的 function annotation bundle。

当前唯一可执行方法：

- `function_annotation.candidate.diamond`

仍为 candidate-only：

- `interproscan`
- `eggnog_mapper`
- `kofamscan`

## 当前实现边界

DIAMOND 已接入 freeze-run-collect：

```text
resolve_protein_inputs
  -> plan_function_methods
  -> validate_database_provenance
  -> freeze_diamond_attempt
  -> run_diamond_attempt
  -> collect_diamond_artifacts
  -> commit_function_annotation
```

DIAMOND 命令形态：

```bash
diamond makedb --in <database.faa> --db <database.dmnd>
diamond blastp --db <database.dmnd> --query <proteins.fa> --outfmt 6 --out <diamond_hits.tsv>
```

collect 阶段注册：

- `function_annotation.diamond_hits`
- `diamond_hits_ref`

## 已修正的 bundle 契约问题

function annotation 曾保留 planning-era 的占位发布模式：`commit_function_annotation` 在 DIAMOND 已执行时会优先使用 `diamond_hits_ref`，但 GO/KEGG/Pfam/annotated GFF 等字段仍可能来自 expected output 占位路径。

这与 gene annotation 之前的问题同型：真实产物和规划占位没有彻底分离。当前实现已经把 bundle 字段改为只发布真实存在的 materialized refs：

- `diamond_hits_ref` 用于 DIAMOND raw outfmt 6 hits。
- `gene_function_tsv_ref` / `go_annotation_table_ref` / `kegg_annotation_table_ref` / `pfam_interpro_table_ref` / `annotated_gene_models_gff3_ref` 只有在对应文件真实存在时才发布。
- 未执行或未产出的结果在 bundle 和 stage output 中为 `null`，而不是 expected output 占位路径。

当前已引入方法级 result bundle，DIAMOND 会写入一个 `FunctionMethodResult`。后续方法切片应复用同一结构，而不是继续把所有方法结果压平到 stage output。

## FunctionMethodResult 契约

方法级结果结构：

```text
FunctionMethodResult
  method_name
  status
  result_refs
  database_provenance
  execution_result_ref
  warnings
```

DIAMOND method result 当前包含：

- `result_refs.diamond_hits`
- `metadata.outfmt`

InterProScan / eggNOG / KOfamScan 后续切片各自扩展 result refs，但都通过同一结构进入最终 bundle。

## FunctionAnnotationBundle 契约

最终 bundle 应只发布真实存在的结果。

必须包含：

- `proteins_fasta_ref`
- `diamond_hits_ref` when DIAMOND executed
- `method_results`
- `database_provenance`
- `metadata.execution_enabled`（执行状态信号，DIAMOND 已产出 hits 时为 `true`）

方法级 warnings 写入 `method_results[].warnings`，bundle 本身没有顶层 `warnings` 字段。

可选包含：

- `gene_function_tsv_ref`
- `go_annotation_table_ref`
- `kegg_annotation_table_ref`
- `pfam_interpro_table_ref`
- `annotated_gene_models_gff3_ref`

可选字段只有在真实文件存在并注册后才能发布。若某类结果未执行，应以 `method_results` 和 warnings 表达“未执行/未产出”，不能发布 expected output 占位路径。

## Database Provenance 契约

来源门控保留，但应按 selected/executable method 判断。

规则：

- `database_provenance` 必须是非空列表。
- 每条记录必须包含 method name、database name、version、source ref、snapshot 或等价版本信息。
- `database_source_ref` 必须存在。
- 若 provenance 省略 `input_proteins_ref`，validation 阶段采用实际 resolved `proteins_fasta_ref` 并回填。
- 若 provenance 显式写了 `input_proteins_ref`，必须与 resolved proteins ref 完全一致。

task 08 应优先使用“省略 `input_proteins_ref`”的模式，避免预猜 gene stage 工作目录路径。

## DIAMOND 契约

DIAMOND 只负责产生 raw hits TSV。它不等价于完整 GO/KEGG/Pfam 注释。

验收应检查：

- `diamond makedb` 和 `diamond blastp` attempt 均记录在 ledger。
- `diamond_hits.tsv` 真实存在且非空。
- `function_annotation.bundle` 记录 DIAMOND database provenance。
- bundle 不把 DIAMOND hits 冒充 GO/KEGG/Pfam/InterPro 输出。

## 与 Gene Annotation 的交界

function annotation 的蛋白输入必须来自 gene annotation 最终输出：

```text
gene_annotation_bundle.proteins_fasta_ref
  -> function_annotation.proteins_fasta_ref
  -> provenance[*].input_proteins_ref
  -> DIAMOND query
```

若 gene annotation 未能物化 proteins FASTA，function annotation 应 terminal fail，错误为 `protein artifact not found for function_annotation`。

## 测试重构设计

建议把测试分为：

- `test_function_annotation_provenance.py`: provenance schema、自动回填 proteins ref、显式不匹配失败。
- `test_function_annotation_diamond.py`: DIAMOND freeze-run-collect、失败清理、artifact 注册。
- `test_function_annotation_bundle.py`: 只发布真实 materialized refs，禁止 expected output 占位；DIAMOND hits 不冒充 GO/KEGG/Pfam。
- `test_function_annotation_e2e.py`: fake DIAMOND + gene proteins handoff + final report。

当前 `tests/test_function_annotation.py` 和 `tests/test_function_annotation_diamond.py` 已覆盖 bundle 不发布未物化 refs、DIAMOND hits 写入 `FunctionMethodResult` 的基础行为；后续可按上面的分层继续拆分。

## 验收

本任务完成时必须满足：

- DIAMOND 能以真实 proteins FASTA 和真实 database FASTA 产出 `diamond_hits.tsv`。
- `function_annotation_bundle.json` 只发布真实存在的 result refs。
- DIAMOND hits 写入 `diamond_hits_ref`，不写入 `gene_function_tsv_ref`。
- DIAMOND result 写入 `method_results[]`，并携带 database provenance。
- provenance 中省略 `input_proteins_ref` 时能回填 gene stage resolved proteins ref。
- provenance 显式路径不匹配时 terminal fail。
- DIAMOND hits 不被误标为 GO/KEGG/Pfam/InterPro 输出。
- task 08 能在 HPC 上验证 DIAMOND provenance、attempt ledger、bundle 和 final report。
