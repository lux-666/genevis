# 07-06 功能注释执行切片

任务状态：规划中；依赖 task 05（最终基因集的 predicted proteins FASTA）

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

1. **添加 `freeze_diamond_attempt` 节点**
   - 前置条件：`stage_outputs.function_annotation.status == "provenance_validated"`（即来源门控已通过）。
   - 输入：`input_proteins_ref`（来自 `stage_output`，由来源门控节点写入）；数据库路径从 manifest `protein_database` asset 的 `provenance.path` 读取。
   - 构造 shell 脚本，命令为：
     ```
     diamond blastp --db <db.dmnd> --query <proteins.fa> --outfmt 6 --out <output.tsv> --threads <N>
     ```
   - 使用 `FrozenAttemptSpec` 封装，`attempt_id` 格式：`{session_id}:function_annotation:diamond`。
   - 调用 `resolve_method_env` + `attach_conda_fields` 为 diamond 方法解析 conda 环境。
   - 成功后将 `status` 置为 `"diamond_frozen"`，写入 `diamond_frozen_attempt`。

2. **添加 `run_diamond_attempt` 节点**
   - 前置条件：`status == "diamond_frozen"`。
   - 调用 `LocalRunner().run(attempt)`，先通过 `Ledger` 记录 `attempt_started`。
   - 失败：清理 expected_outputs，记录 `attempt_failed`（含 return_code + stderr），将 `status` 置为 `"failed"`，写入 `reason`。
   - 成功：将 `status` 置为 `"diamond_ran"`，写入 `diamond_execution_result`。

3. **添加 `collect_diamond_artifacts` 节点**
   - 前置条件：`status == "diamond_ran"`。
   - 将结果 TSV 注册为 `ArtifactMeta`，`artifact_type="function_annotation.diamond_hits"`。
   - 调用 `append_artifact_records` + `Ledger.record_artifact_registered`。
   - 将 `diamond_hits_ref` 写入 `stage_outputs.function_annotation`，`status` 置为 `"diamond_completed"`。

4. **打开 diamond 方法的 `execution_enabled` 门控**
   - 在 `freeze_diamond_attempt` 中，对 diamond 方法不设置 `execution_enabled: False`；若 `stage_outputs` 状态不是 `provenance_validated` 则直接返回 `{}`（即来源门控充当唯一前置开关）。
   - `commit_function_annotation` 中 `metadata.execution_enabled` 的全局 False 不影响 diamond 子路径，diamond 独立提交自己的 artifact。

5. **将 diamond 的 `execution_support` 改为 `executable`**
   - 修改 `app/entrypoints/ask_planner.py:310-315`，将 `function_annotation.candidate.diamond` 的 `execution_support` 由 `"planned_only"` 改为 `"executable"`。

6. **在 `config/envs.yaml` 中添加 diamond 方法条目**
   - 在 `methods:` 节下新增：
     ```yaml
     function_annotation.candidate.diamond:
       conda_env: env_geno_exec
       required_tools:
         - diamond
     ```

7. **接入 `resolve_method_env` / `attach_conda_fields`**
   - `freeze_diamond_attempt` 节点内调用方式与 `freeze_flye_attempt`（nodes.py:311-320）完全一致：传入 `stage="function_annotation"`、`method_name="function_annotation.candidate.diamond"`。

8. **将来源门控设为 diamond freeze 的硬前置条件**
   - `freeze_diamond_attempt` 节点首先检查 `status == "provenance_validated"`；如不满足直接返回 `{}`，不构造脚本。
   - 来源门控节点本身不变——不为 diamond 添加任何新的绕过逻辑。

9. **添加单元测试**（`tests/test_function_annotation_diamond.py`）
   - `test_freeze_produces_correct_diamond_command`：mock `resolve_method_env`，验证脚本含正确的 `diamond blastp --db ... --query ... --outfmt 6 --out ...`。
   - `test_runner_failure_records_failed_attempt`：mock `LocalRunner().run` 返回失败，验证 `Ledger.record_attempt_failed` 被调用，`status == "failed"`。
   - `test_collect_registers_diamond_hits_artifact`：给定 `status == "diamond_ran"` 且结果文件存在，验证 `artifact_type == "function_annotation.diamond_hits"`，`stage_outputs` 中写入 `diamond_hits_ref`。
   - `test_provenance_gate_blocks_when_database_missing`：来源记录中 `path` 指向不存在文件，验证 `validate_database_provenance` 返回失败状态，`freeze_diamond_attempt` 节点不被触发。

10. **真实输入冒烟验证**
    - 输入：task 05 生成的海豚 predicted proteins FASTA（`proteins.fa`）；本地 SwissProt `.dmnd` 数据库。
    - 手工构造包含正确来源记录的 manifest，执行端到端流程。
    - 验证：`diamond_hits_ref` 文件存在、非空，artifact 记录写入 ledger，`status == "diamond_completed"`。

## 后续切片

| 切片 | 方法 | 主要依赖 |
|---|---|---|
| Slice 2 | InterProScan | Pfam/InterPro 数据库本地安装；输出含 GO term |
| Slice 3 | eggNOG-mapper | eggNOG 数据库（`eggnog.db` + diamond 索引）；输出含 COG/KEGG |
| Slice 4 | KOfamScan | KOfam HMM profiles + ko_list；输出含 KEGG KO 号 |

每个切片依照相同的 freeze→run→collect 模式；来源门控保持为统一前置条件。

## 验收

- [ ] `freeze_diamond_attempt` 构造的脚本包含正确的 `diamond blastp` 命令（db / query / outfmt 6 / out 参数均正确）。
- [ ] `run_diamond_attempt` 失败时，expected outputs 被清理，`Ledger` 记录失败条目，`status == "failed"`。
- [ ] `collect_diamond_artifacts` 将结果 TSV 注册为 `function_annotation.diamond_hits` artifact，`stage_outputs.function_annotation.diamond_hits_ref` 有效。
- [ ] 当 manifest 缺少 `protein_database` asset 或文件不存在时，`validate_database_provenance` 阻断流程，`freeze_diamond_attempt` 不执行。
- [ ] `config/envs.yaml` 中存在 `function_annotation.candidate.diamond` 条目，`required_tools: [diamond]`。
- [ ] `ask_planner.py` 中 diamond 的 `execution_support` 为 `"executable"`。
- [ ] 全部新增单元测试通过（`pytest tests/test_function_annotation_diamond.py`）。
- [ ] 真实海豚数据冒烟验证：`diamond_hits_ref` 文件非空，artifact 写入 ledger。
