# 07-04 基因注释执行切片

任务状态：规划中；依赖 task 03 的注释 review chain。

## 目标

为基因注释预测器逐方法添加真实执行能力，每次一个切片，复用装配主线已验证的 runner 模式。每个切片将一个当前 `planned_only` 的方法推进到真正可执行状态。

## 当前状态（已验证）

- `gene_annotation` 阶段目前全部为规划 + 采用模式：无 `run_*_attempt`、无 `LocalRunner`、无子进程调用。所有提交路径均设置 `execution_enabled: False`，备注 `execution_deferred_to_method_specific_task`（`app/stages/gene_annotation/nodes.py`）。
- 注册表中所有方法均为 `planned_only`（`app/entrypoints/ask_planner.py:247-296`）：`braker3`、`maker`、`augustus`、`miniprot`、`hisat2`、`stringtie`、`isoquant`、`gemoma`。
- 待复制的 runner 模式：`run_flye_attempt`（`app/stages/assembly/nodes.py:381`）与 `run_yahs_attempt`（`app/stages/hic_scaffolding/nodes.py:251`）——冻结 `FrozenAttemptSpec` → `LocalRunner().run(...)` → 收集 artifacts → 注册 → ledger 事件 → 通过 `resolve_method_env` / `attach_conda_fields` 挂载 conda 环境。

## 范围

- 每个切片实现一个基因注释方法的真实本地执行，产出持久化候选基因模型 artifact。
- 向 task 03（候选证据 review chain）提供真实候选结果。

## 非目标

- 候选合并 / 最终基因集生成（task 05）。
- 一次性实现所有方法；切片相互独立、可单独验收。

## 设计方向

**切片顺序**：

- **Slice 1：Miniprot**——蛋白同源注释，输入面最简（一个蛋白 FASTA + 一个基因组 FASTA），直接输出 GFF3，无需物种训练，比 BRAKER3 轻量。
- **Slice 2**：AUGUSTUS 或 BRAKER3（ab initio 或带证据训练路径）。

**候选基因模型表示**：在 `gene_annotation` 的 `stage_output` 中新增 `candidate_gff_ref` 字段，同时注册持久化 artifact `gene_annotation.candidate_gff`。

**Conda 环境**：在 `config/envs.yaml` 的 `methods` 节下新增 `gene_annotation.candidate.miniprot` 条目，指向包含 miniprot 的 conda 环境（初始复用 `env_geno_exec`，如需独立环境可切换为 `conda_prefix`）。

## 任务

### Slice 1：Miniprot

- [ ] **添加 `freeze_miniprot_attempt` 节点**：从 `stage_outputs.gene_annotation` 读取 `genome_input_ref`（mask_review 产出的软屏蔽基因组）与 manifest asset `protein_db` 路径；构造命令 `miniprot --gff <genome> <proteins>`；写入 shell 脚本；调用 `resolve_method_env` / `attach_conda_fields`；生成 `FrozenAttemptSpec`，状态置为 `miniprot_frozen`。
- [ ] **添加 `run_miniprot_attempt` 节点**：校验状态 `miniprot_frozen`；调用 `LocalRunner().run(attempt)`；失败时清理预期输出文件、调用 `ledger.record_attempt_failed`、返回 `status: failed`；成功时状态置为 `miniprot_ran`。
- [ ] **添加 `collect_miniprot_artifacts` 节点**：校验状态 `miniprot_ran`；将产出 GFF3 文件以 artifact 类型 `gene_annotation.candidate_gff` 注册到 artifact 账本；在 `stage_output` 中写入 `candidate_gff_ref`；状态置为 `miniprot_collected`。
- [ ] **将三个节点连入 `gene_annotation` 图**：在现有规划阶段节点（`plan_evidence_merge_or_review`）之后按顺序连接 `freeze_miniprot_attempt` → `run_miniprot_attempt` → `collect_miniprot_artifacts`；仅当 selected method 为 miniprot 时路由进入此分支。
- [ ] **将 `miniprot` 注册表条目 `execution_support` 翻转为 `executable`**：修改 `app/entrypoints/ask_planner.py` 中 `gene_annotation.candidate.miniprot` 对应条目。
- [ ] **在 `config/envs.yaml` 中添加 `gene_annotation.candidate.miniprot` conda 环境条目**：在 `methods` 节新增条目，`conda_env: env_geno_exec`（或独立的 miniprot 专属环境），`required_tools: [miniprot]`。
- [ ] **连接 `resolve_method_env` / `attach_conda_fields`**：在 `freeze_miniprot_attempt` 中调用方式与 `freeze_flye_attempt` 一致，确保 conda 环境字段正确注入 `FrozenAttemptSpec`。
- [ ] **添加单元测试**（`tests/test_gene_annotation_miniprot.py` 或扩展现有测试文件）：
  - `freeze_miniprot_attempt` 产出的 `FrozenAttemptSpec` 命令行包含正确的 `--gff <genome> <proteins>` 参数；
  - `run_miniprot_attempt` 在 runner 返回失败时记录 `attempt_failed` ledger 事件、`stage_output.status` 为 `failed`；
  - `collect_miniprot_artifacts` 正确注册 `gene_annotation.candidate_gff` artifact；
  - 执行完整 Slice 1 路径后 `stage_output` 含 `candidate_gff_ref`。
- [ ] **本地冒烟验证**：在真实海豚软屏蔽基因组 + 真实蛋白数据库上执行完整 Slice 1 路径，确认 GFF3 产出并正确注册。
- [ ] **记录后续切片入口**（见下方 ## 后续切片）。

## 后续切片

**Slice 2：AUGUSTUS 或 BRAKER3**

- 若无外部证据（`evidence_profile: no_evidence`），以 AUGUSTUS 作为 ab initio 切片；若有 RNA-seq / 蛋白证据，优先 BRAKER3。
- 输入面：屏蔽基因组 + 物种/谱系参数（AUGUSTUS）或 + RNA-seq BAM / 蛋白 FASTA（BRAKER3）。
- 实现模式与 Slice 1 相同：freeze → run → collect，conda 环境独立条目，`execution_support` 翻转，新增测试。
- 设计细节在 Slice 1 完成后具体化。

## 验收

- `gene_annotation.candidate.miniprot` 在注册表中 `execution_support` 为 `executable`。
- `config/envs.yaml` 中存在 `gene_annotation.candidate.miniprot` 条目，`required_tools` 包含 `miniprot`。
- 以真实海豚屏蔽基因组 + 蛋白数据库触发 Slice 1 路径，可产出可读取的 GFF3 文件。
- artifact 账本中出现类型为 `gene_annotation.candidate_gff` 的记录，路径指向上述 GFF3。
- `stage_outputs.gene_annotation` 中 `candidate_gff_ref` 字段非空，指向同一 GFF3。
- runner 失败场景（模拟 miniprot 非零返回码）：ledger 中出现 `attempt_failed` 事件，`stage_output.status` 为 `failed`，预期输出文件已清理。
- 所有新增单元测试通过（`pytest tests/test_gene_annotation_miniprot.py` 或对应文件）。
- 现有测试套件无回归（`pytest tests/`）。
