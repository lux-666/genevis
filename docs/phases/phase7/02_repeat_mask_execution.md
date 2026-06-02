# 07-02 重复序列/掩蔽执行端到端

任务状态：规划中；依赖 task 01 的通用 review-gate 机制。

## 目标

通过使用现有重复序列库（例如 Dfam 衍生库）运行 RepeatMasker，为真实基因组（海豚组装是当前驱动案例）产出真实的掩蔽基因组；随后通过 task 01 的通用 gate 机制呈现一个掩蔽完成审阅，告诉操作者哪些步骤已运行、哪些没有运行，以及推荐用于基因注释的输入。

## 当前状态（已验证）

- `run_repeatmasker_attempt`（`app/stages/repeat_annotation/nodes.py:312`）是真实 runner：它调用 `LocalRunner().run(attempt)`，并且已经接入图中。
- 冻结后的命令是 `RepeatMasker -lib <library_ref> -gff -xsmall <asm>`（`nodes.py:273`）。`library_ref` 必须是一个**实际存在的本地 FASTA 文件**（`nodes.py:244`），并且来自 `asset_type == "repeat_library"` 的 manifest asset（`nodes.py:586 -> 110`）。
- 执行受 `strategy == "provided"` 约束（`nodes.py:537`），而 `strategy` 只有在存在这类 asset 时才会是 `"provided"`（`nodes.py:87`）。
- 从头构建重复序列库（EDTA / RepeatModeler）和 TRF 目前都只是规划态。

## Dfam：两条可选路线（先决策）

- **路线 A：Dfam -> FASTA -> provided library（零代码改动）。** 使用 `famdb.py` 导出相关 Dfam 谱系（对海豚而言是 Cetacea / Laurasiatheria；预计不会有 *Sousa chinensis* 的物种特异库）为 library FASTA，将其注册为 `repeat_library` manifest asset，现有可执行路径即可原样运行。
- **路线 B：RepeatMasker 内置 Dfam / `-species`（需要代码改动）。** 教会 freeze 步骤使用第二种模式：使用 RepeatMasker 捆绑的 Dfam 或 `-species` 参数，而不是 `-lib <file>`。这会对命令构建器产生更大的改动。

推荐先从路线 A 开始：它不需要改 runner，就能让海豚案例真实跑起来。

## 范围

- 通过路线 A（或在选定后通过路线 B）端到端产出真实的掩蔽基因组。
- 增加一个**掩蔽完成审阅** gate，作为 task 01 gate registry 的消费者，报告内容例如：
  - known RepeatMasker：已完成
  - de novo library：未运行
  - TRF：未运行
  - masked genome：`<softmasked_genome path>`
  - repeat percentage：`xx%`
  - recommended gene input：下游预测使用该掩蔽基因组
- 诚实标注重复序列库来源（例如“近缘谱系 Dfam 库，而非物种特异库”），避免夸大掩蔽完整性。
- 将 mask review 的 decision/recommendation/resume/ledger 记录纳入与其它 gates 相同的 trace 形态，使 task 03 可以把它作为 annotation review chain 的第一个注释阶段 gate。

## 非目标

- 从头构建重复序列库（EDTA / RepeatModeler）和 TRF；这些属于后续独立切片。
- 改动基因/功能注释执行；这些属于独立任务。
- 自行实现专用 review/resume 机制；必须复用 task 01 的通用 gate 机制。

## 待决问题

- Dfam 选择路线 A 还是路线 B。
- 重复序列比例从哪里来（RepeatMasker `.tbl` summary？），以及是否需要持久化解析。
- mask review 的默认 wait policy：默认自动通过、限时等待，还是人工确认。
