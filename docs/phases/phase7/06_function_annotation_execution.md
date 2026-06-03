# 07-06 功能注释执行切片

任务状态：Slice 1 (DIAMOND) 本地实现完成（未提交）；依赖 task 05（最终基因集的 predicted proteins FASTA）

## 实现要点（本地）

- 三个 shared helpers（`_freeze_func_method_attempt` / `_run_func_method_attempt` / `_collect_func_method_artifacts`）遵循 gene_annotation 的 freeze→run→collect 模式，为后续切片（InterProScan / eggNOG-mapper / KOfamScan）预留复用。
- `FUNCTION_EXECUTABLE_METHODS` 配置 dict（与 `GENE_EXECUTABLE_METHODS` 同模式），DIAMOND 为当前唯一条目。
- `freeze_diamond_attempt` 构造 `diamond makedb` + `diamond blastp` 两个命令，输出 outfmt 6 TSV。无 diamond 来源记录时跳过（`diamond_completed` 直通），不阻塞未配置 diamond 的流程。
- `resolve_protein_inputs` 添加 guard：若 `function_output.status` 已存在则返回 `{}`，防止 crash-recovery 覆盖已有中间状态。
- `commit_function_annotation` 在 `diamond_hits_ref` 存在时优先使用实际输出路径而非 planned path。
- DIAMOND 命令参数：`--db`（从 dmnd_index）、`--query`（proteins FASTA）、`--outfmt 6`、`--out`（diamond_hits.tsv）。
- 来源门控保持不变：`validate_database_provenance` 仍要求所有方法提供来源记录。
- crash-recovery：`route_after_provenance_validation` 识别 `diamond_frozen` / `diamond_ran` / `diamond_completed` 中间状态并路由至正确节点。
- `config/envs.yaml` 新增 `function_annotation.candidate.diamond` 条目（`conda_env: env_geno_exec`，`required_tools: [diamond]`）。
- `ask_planner.py` 中 diamond `execution_support` 改为 `executable`。

## 目标

逐方法为功能注释阶段添加真实执行能力，每次实现一个方法，镜像装配主线模式（freeze→run→collect），同时保留现有数据库来源门控作为前置条件。

## 当前状态（已验证）

- `function_annotation` 阶段目前仅完成规划与来源门控，无任何 runner 或 subprocess 调用。所有提交路径均设置 `execution_enabled: False`（`app/stages/function_annotation/nodes.py`，`FunctionAnnotationPlan.metadata`）。
- `validate_database_provenance`（nodes.py:109）仅校验 `database_provenance` 记录列表 + `input_proteins_ref` 指向已落盘的 FASTA 文件，不运行任何工具。校验通过后 `stage_outputs.function_annotation.status` 变为 `"provenance_validated"`。
- 注册方法，全部为 `planned_only`（`app/entrypoints/ask_planner.py:297-322`）：`interproscan`、`eggnog_mapper`、`diamond`、`kofamscan`。
- 复用的 runner 模式：与 task 04（Flye/YaHS freeze→run→collect）相同，见 `app/stages/assembly/nodes.py:291-430`。
- `config/envs.yaml` 当前无 `function_annotation` 阶段或方法级条目；所有工具共用 `env_geno_exec`。

## 范围

在来源验证通过之后，对 predicted proteins FASTA 执行一个功能注释方法（Slice 1：DIAMOND），生成可持久化的注释结果 TSV，并注册为 artifact。

## 非目标

- 基因注释 / 合并（task 04-05）。
- 放宽或移除数据库来源要求——门控保持不变。
- 同时实现多个方法（每个切片只做一个）。
- 数据库自动下载（禁用，来源必须由用户手工提供）。

## 切片策略

- 每个切片实现一个方法，四个方法相互独立（各自在不同维度上注释同一份 proteins）。
- 每个切片：freeze + runner + collect，conda env 条目，将对应方法的 `execution_support` 从 `planned_only` 改为 `executable`，保留来源门控作为前置条件，单元测试，真实输入冒烟验证，最后 HPC 适配。
- 第一切片：**DIAMOND**——速度最快，DB 面最简单（单个 FASTA 蛋白数据库，如 SwissProt），本地可用小型 DB 文件即可验证。

## 已决事项

| 决策点 | 结论 |
|---|---|
| 第一切片方法 | DIAMOND（blastp，Tab 格式输出，outfmt 6） |
| 数据库类型 | 蛋白序列 FASTA（如 SwissProt），用 `diamond makedb` 预构建为 `.dmnd` |
| 来源记录方式 | manifest 中须包含 `asset_type: protein_database`，含 `provenance` 字段：`{"source": "swissprot", "version": "2024-01", "path": "/data/db/swissprot.fasta"}`；`validate_database_provenance` 负责检查该字段及文件是否存在 |
| conda env 键名 | `function_annotation.candidate.diamond`，值 `conda_env: env_geno_exec`，`required_tools: [diamond]` |
| 输出 artifact 键 | `function_annotation.diamond_hits`，`stage_output` 中写入 `diamond_hits_ref` |
| execution_enabled 开关 | freeze 节点内对 diamond 方法做方法级判断，替换全局 `execution_enabled: False` 为 `method == "diamond"` 条件分支 |

## 任务（Slice 1: DIAMOND）

- [x] **添加 `freeze_diamond_attempt` 节点**
   - 前置条件：`stage_outputs.function_annotation.status == "provenance_validated"`（即来源门控已通过）。
   - 输入：`input_proteins_ref`（来自 `stage_output`，由来源门控节点写入）；数据库路径从 manifest `protein_database` asset 的 `provenance.path` 读取。
   - 构造 shell 脚本，命令为：
     ```
     diamond blastp --db <db.dmnd> --query <proteins.fa> --outfmt 6 --out <output.tsv> --threads <N>
     ```
   - 使用 `FrozenAttemptSpec` 封装，`attempt_id` 格式：`{session_id}:function_annotation:diamond`。
   - 调用 `resolve_method_env` + `attach_conda_fields` 为 diamond 方法解析 conda 环境。
   - 成功后将 `status` 置为 `"diamond_frozen"`，写入 `diamond_frozen_attempt`。

- [x] **添加 `run_diamond_attempt` 节点** — 通过 shared helper `_run_func_method_attempt` 执行，失败清理 expected_outputs 并记录 `attempt_failed`。

- [x] **添加 `collect_diamond_artifacts` 节点** — 通过 shared helper `_collect_func_method_artifacts` 注册 `function_annotation.diamond_hits` artifact，写入 `diamond_hits_ref`。

- [x] **打开 diamond 方法的 `execution_enabled` 门控**
   - 在 `freeze_diamond_attempt` 中，对 diamond 方法不设置 `execution_enabled: False`；若 `stage_outputs` 状态不是 `provenance_validated` 则直接返回 `{}`（即来源门控充当唯一前置开关）。
   - `commit_function_annotation` 中 `metadata.execution_enabled` 的全局 False 不影响 diamond 子路径，diamond 独立提交自己的 artifact。

- [x] **将 diamond 的 `execution_support` 改为 `executable`** — `app/entrypoints/ask_planner.py`

- [x] **在 `config/envs.yaml` 中添加 diamond 方法条目** — `function_annotation.candidate.diamond`，`conda_env: env_geno_exec`，`required_tools: [diamond]`

- [x] **接入 `resolve_method_env` / `attach_conda_fields`** — 与 assembly/gene_annotation 模式完全一致

- [x] **将来源门控设为 diamond freeze 的硬前置条件** — `freeze_diamond_attempt` 检查 `status == "provenance_validated"`，不满足直接返回 `{}`。无 diamond 来源时跳过（`diamond_completed` 直通）

- [x] **添加单元测试** — `tests/test_function_annotation_diamond.py`（11 tests）：freeze（4）、run（3）、collect（3）、全路径（1）

- [ ] **真实输入冒烟验证**

## 后续切片

| 切片 | 方法 | 主要依赖 |
|---|---|---|
| Slice 2 | InterProScan | Pfam/InterPro 数据库本地安装；输出含 GO term |
| Slice 3 | eggNOG-mapper | eggNOG 数据库（`eggnog.db` + diamond 索引）；输出含 COG/KEGG |
| Slice 4 | KOfamScan | KOfam HMM profiles + ko_list；输出含 KEGG KO 号 |

每个切片依照相同的 freeze→run→collect 模式；来源门控保持为统一前置条件。

## 验收

- [x] `freeze_diamond_attempt` 构造的脚本包含正确的 `diamond blastp` 命令（db / query / outfmt 6 / out 参数均正确）。
- [x] `run_diamond_attempt` 失败时，expected outputs 被清理，`Ledger` 记录失败条目，`status == "failed"`。
- [x] `collect_diamond_artifacts` 将结果 TSV 注册为 `function_annotation.diamond_hits` artifact，`stage_outputs.function_annotation.diamond_hits_ref` 有效。
- [x] 当 manifest 缺少 diamond 来源记录时，`freeze_diamond_attempt` 跳过（`diamond_completed` 直通），不阻断流程。
- [x] `config/envs.yaml` 中存在 `function_annotation.candidate.diamond` 条目，`required_tools: [diamond]`。
- [x] `ask_planner.py` 中 diamond 的 `execution_support` 为 `"executable"`。
- [x] 全部新增单元测试通过（`pytest tests/test_function_annotation_diamond.py` — 11 tests）。
- [ ] 真实海豚数据冒烟验证：`diamond_hits_ref` 文件非空，artifact 写入 ledger。
