# 07-02 重复序列/掩蔽执行端到端

任务状态：本地实现完成（commit f5fafa4）。

## 目标

通过使用现有重复序列库（例如 Dfam 衍生库）运行 RepeatMasker，为真实基因组（海豚组装是当前驱动案例）产出真实的掩蔽基因组；随后通过 task 01 的通用 gate 机制呈现一个掩蔽完成审阅，告诉操作者哪些步骤已运行、哪些没有运行，以及推荐用于基因注释的输入。

## 当前状态（已验证）

- `run_repeatmasker_attempt`（`app/stages/repeat_annotation/nodes.py:312`）是真实 runner：它调用 `LocalRunner().run(attempt)`，并且已经接入图中。
- 冻结后的命令是 `RepeatMasker -lib <library_ref> -gff -xsmall <asm>`（`nodes.py:273`）。`library_ref` 必须是一个**实际存在的本地 FASTA 文件**（`nodes.py:244`），并且来自 `asset_type == "repeat_library"` 的 manifest asset（`nodes.py:586 -> 110`）。
- 执行受 `strategy == "provided"` 约束（`nodes.py:537`），而 `strategy` 只有在存在这类 asset 时才会是 `"provided"`（`nodes.py:87`）。
- `collect_repeatmasker_artifacts`（`nodes.py:346`）已收集 `softmasked_genome`、`repeat_annotation_gff3`、可选的 `hardmasked_genome`，并写入 ledger；但**尚未收集 `.tbl` 摘要文件，也没有 `mask_review` gate**。
- 从头构建重复序列库（EDTA / RepeatModeler）和 TRF 目前都只是规划态。

## 已决事项

- **Dfam 路线选择：路线 A（零代码改动）。** 使用 `famdb.py` 导出 Cetacea / Laurasiatheria 谱系为 FASTA，注册为 `repeat_library` manifest asset，现有可执行路径原样运行。路线 B（`-species` / RepeatMasker 内置 Dfam）保留为后续独立选项，本任务不涉及。
- **重复序列比例来源：解析 RepeatMasker `.tbl` 摘要文件。** RepeatMasker 在工作目录写出 `<assembly>.tbl`；从其中的 `"bases masked"` 行提取百分比；若 `.tbl` 缺失则以 `None` 上报，不阻断流程。
- **`mask_review` 默认 wait policy：自动通过（`auto_decidable=True`，无等待）。** gate 的目的是溯源和操作员覆盖，不是强制人工确认。允许的决策为 `accept`（使用掩蔽基因组）和 `skip`（回退原始组装）。

## 范围

- 通过路线 A 端到端产出真实的掩蔽基因组。
- 在 `collect_repeatmasker_artifacts` 后解析 `.tbl` 摘要，提取 `repeat_percentage` 并写入 stage 输出。
- 增加一个 **`mask_review` gate**，作为 task 01 gate registry 的消费者，报告内容包括：
  - known RepeatMasker：已完成
  - de novo library：未运行
  - TRF：未运行
  - masked genome：`<softmasked_genome 路径>`
  - repeat percentage：`xx%`（来自 `.tbl`，缺失时为 `null`）
  - library source label：例如"近缘谱系 Dfam 库（Cetacea/Laurasiatheria），非物种特异库"
  - recommended gene input：下游预测使用该掩蔽基因组
- 诚实标注重复序列库来源，避免夸大掩蔽完整性。
- 将 `mask_review` 的 decision / recommendation / resume / ledger event 纳入与其它 gates 相同的 trace 形态，使 task 03 可以把它作为 annotation review chain 的第一个注释阶段 gate。

## 非目标

- 从头构建重复序列库（EDTA / RepeatModeler）和 TRF；这些属于后续独立切片。
- 路线 B（`-species` / RepeatMasker 内置 Dfam）；后续独立切片。
- 改动基因/功能注释执行；这些属于独立任务。
- 自行实现专用 review/resume 机制；必须复用 task 01 的通用 gate 机制。

## 任务

- [ ] **Dfam 库准备（操作步骤，非代码改动）：** 使用 `famdb.py` 从本地 Dfam 数据库导出 Cetacea / Laurasiatheria 谱系为 FASTA，路径写入 manifest 的 `repeat_library` asset；确认 `select_repeat_strategy` 选出 `"provided"` 分支。
- [ ] **解析 `.tbl` 摘要，提取 `repeat_percentage`：** 在 `collect_repeatmasker_artifacts` 内（或紧随其后的新节点）查找工作目录下的 `assembly.fasta.tbl`，用正则提取 `"bases masked"` 行百分比，写入 stage 输出字段 `repeat_percentage`（`float | None`）；`.tbl` 缺失时写 `None`，不阻断后续流程。
- [ ] **在 `app/review/gates.py` 中注册 `mask_review` gate：** 调用 `register_review_gate(ReviewGateDeclaration(stage="repeat_annotation", gate_id="mask_review", decisions_allowed=("accept", "skip"), decision_applier="app.stages.repeat_annotation.nodes:apply_mask_review_decision", template_preferred_decision="accept"))`；不需要专用 `prompt_bundle`，走通用 prompt adapter。
- [ ] **构建 `ReviewPayload` builder：** 在 `app/stages/repeat_annotation/nodes.py` 中新增 `build_mask_review_payload(state)` 函数，从 stage 输出收集 `softmasked_genome_ref`、`repeat_percentage`、`library_source` 标签、已运行/跳过步骤摘要，填充 `ReviewPayload`（`auto_decidable=True`，`decisions_allowed=["accept","skip"]`）并写入 `active_interrupt`。
- [ ] **在图中接入 gate 触发点：** 在 `collect_repeatmasker_artifacts` 节点完成后、`commit_repeat_annotation` 之前，插入 `build_mask_review_payload` 调用；gate 状态写入 `active_interrupt`，由现有 policy engine 决定是否阻断或自动通过。
- [ ] **实现 `apply_mask_review_decision`：** `accept` 分支确认 `softmasked_genome_ref` 传递给下游（保持现有路径）；`skip` 分支将 `softmasked_genome_ref` 置为原始组装 ref，并在 notes 中追加 `"mask_review_skipped"`；两条分支均清除 `active_interrupt`。
- [ ] **持久化 `mask_review` 决策 artifact 和 ledger event：** 在 `apply_mask_review_decision` 中写入与 `assembly_review` / `hic_scaffolding` 相同格式的 ledger event；将决策 JSON 写为 artifact 并追加到 `artifact_refs`；`route_trace` 追加 `"mask_review"` 节点。
- [ ] **添加测试：**
  - `mask_review` gate 已在 registry 中声明，`get_review_gate("repeat_annotation", "mask_review")` 不抛出异常。
  - `build_mask_review_payload` 在 `repeat_percentage` 为 `None` 时仍生成合法 payload。
  - `auto_decidable=True` 时 policy engine 返回 `AUTO_DECIDE_NOW`，gate 不阻断流程。
  - 手动 `accept` resume 后 `stage_outputs["repeat_annotation"]["softmasked_genome_ref"]` 保持掩蔽基因组路径。
  - `skip` resume 后 `softmasked_genome_ref` 回退为原始组装 ref，notes 含 `"mask_review_skipped"`。
  - trace 中包含 `"mask_review"` 节点。
- [ ] **本地烟雾测试（海豚组装）：** 以实际 Dfam FASTA 库和海豚组装为输入，端到端运行 `repeat_annotation` 阶段，确认 `softmasked_genome.fasta` 生成、`repeat_percentage` 非空、`mask_review` gate 自动通过，bundle artifact 写出。

## 验收

本任务完成时：

- 以路线 A Dfam FASTA 库为输入，`repeat_annotation` 阶段端到端产出 `softmasked_genome.fasta`，`repeat_percentage` 从 `.tbl` 正确提取并写入 stage 输出（`.tbl` 缺失时为 `None` 而非报错）。
- `mask_review` gate 在 registry 中声明，默认 `auto_decidable=True`，不阻断正常流程；gate payload 包含 softmasked genome 路径、repeat percentage、library source 标签和已运行/跳过步骤摘要。
- `apply_mask_review_decision` 的 `accept` 分支将掩蔽基因组 ref 传递给下游；`skip` 分支回退为原始组装 ref，notes 含 `"mask_review_skipped"`。
- `mask_review` 决策以与 `assembly_review` / `hic_scaffolding` 相同的格式写入 ledger 和 artifact，`route_trace` 含 `"mask_review"` 节点，task 03 可直接读取该决策。
- 全部新增测试通过，现有 `assembly_review` / `hic_scaffolding` 测试无回归。
