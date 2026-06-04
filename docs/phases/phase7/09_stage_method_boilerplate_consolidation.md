# 07-09 Stage 方法样板收敛（freeze/run/collect 节点 + 路由）

任务状态：切片 1 全 stage 完成 + 切片 2 gene_annotation 完成。纯内部重构，行为保持不变，613 passed（仅既有 Flye 环境失败的 7 个 AssemblyRerunTests 除外，与本重构无关）。

## 实现要点（落地记录）

- **落点已定：`app/execution/method_runner.py`**（不重建 forge）。提供
  `build_frozen_attempt(...)`（组装 kwargs + `attach_conda_fields` + `FrozenAttemptSpec`）
  与 `run_frozen_attempt(state, attempt, *, fail_label, cleanup_outputs=True) -> RunOutcome`
  （ledger create/start + `LocalRunner` + 失败时按需 unlink + record_attempt_failed）。
- **切片 1 已覆盖所有 method-bearing stage**：read_qc（seqkit/length_filter）、
  assembly（flye）、genome_survey（jellyfish/genomescope2）、function_annotation
  （diamond）、hic_scaffolding（yahs）、repeat_annotation（repeatmasker）、
  assembly_review（quast/busco 复数 + flye rerun 的 spec 组装）、gene_annotation
  （miniprot/augustus/evm/busco）。collect 路径保持 stage 专属。
- **`cleanup_outputs=False` 的特例**：repeat_annotation（失败注册 log artifacts）、
  hic_scaffolding（失败仍进 yahs_ran，由 collect 决定 fallback）、assembly_review
  quast/busco（失败写入 result_payload["error"] 继续）、gene_annotation busco
  （失败映射为 merge_accepted）。这些 caller 保留各自失败后处理，仅复用 run 核心。
- **切片 2 仅 gene_annotation**：miniprot/augustus 的 4 个薄转发器删除，builder 改用
  `functools.partial(_route_after_freeze, suffix=...)`。其余 stage 当前均为单方法
  （每个 freeze/run/collect 只有一个 router），table 化无去重收益，按"避免过早抽象"
  推迟到该 stage 真正新增第二个方法时再做。

## 目标

把各 stage 中**每加一个方法就要复制一遍**的两类样板收敛为"共享 helper + 每
stage 一张 `{suffix: cfg}` 表"的形态,使新增方法从"复制 ~50 行 freeze/run/collect
节点 + 3 个 route_after 转发器"降为"填一行 cfg + 写脚本正文"。`gene_annotation`
已经是两类收敛的现成模板,本任务是把它的模式平移到其余 method-bearing stage。

这覆盖此前讨论的 A-2(freeze/run 共享骨架)与 routing thin-wrapper 两项,二者是
同一主题、同一模板,合并为一个任务。

## 模板（已存在,勿重写,作为参照）

- 节点侧:`app/stages/gene_annotation/nodes.py:1016` `_freeze_gene_method_attempt`
  / `:1084` `_run_gene_method_attempt` / `:1120` `_collect_gene_method_artifacts`,
  由 `_METHOD_EXEC`(`nodes.py:998` 起的 `{method_name: {suffix, frozen_status,
  ran_status}}`)驱动。
- 路由侧:`app/stages/gene_annotation/routing.py:135` `_route_after_freeze` /
  `:139` `_route_after_run`,每方法仅保留薄转发器(`:153-166`)以满足 LangGraph
  对独立 callable 的要求。

## 背景:重复面

`freeze_*_attempt` 在 8 个 stage 各写一遍同一骨架(resolve env → mkdir
work/artifact → 写 script → `attach_conda_fields` → `FrozenAttemptSpec` → 经
`LocalRunner` 执行 → ledger),仅脚本正文与路径不同:

- `assembly`(flye)、`genome_survey`(jellyfish/genomescope2)、`read_qc`
  (seqkit)、`repeat_annotation`(repeatmasker)、`function_annotation`
  (diamond)、`hic_scaffolding`(yahs)、`assembly_review`(quast/busco)。

路由侧每个 method-bearing stage 都有 `route_after_<method>_{freeze,run,collect}`
三元组,绝大多数是"failed 则 fail,否则固定下一步"的薄转发(见各
`app/stages/*/routing.py`)。

## 范围

两个切片,均行为保持、不改图结构(节点名、边、gate 不变):

**切片 1 — freeze/run/collect 节点骨架共享**
- 把 `gene_annotation` 的三个 `_*_gene_method_attempt` helper 提升为跨 stage 共享
  helper,各 stage 的 `freeze_*/run_*/collect_*` 退化为"提供 cfg + script_body"
  的薄包装。
- 各 stage 维护一张 `{method_name: cfg}` 表(沿用 `_METHOD_EXEC` 形态)。

**切片 2 — 路由表化**
- 每 stage 保留 `_route_after_freeze/_run/_collect(state, *, suffix, ...)` 共享
  helper,薄转发器用 `functools.partial(_route_after_freeze, suffix=...)` 从
  `{suffix: ...}` 表生成并注册,删除手写的 `route_after_<method>_freeze/run` 转发
  函数。
- LangGraph 要求每条 conditional-edge 是独立 callable,`functools.partial` 生成的
  对象彼此 distinct,满足该约束(已在 `gene_annotation` 验证可行)。

## 已决 / 待定

- **共享 helper 落点(已决):** `app/execution/method_runner.py`(不重建 forge)。
  architecture.md / governance.md 的 forge 段落已同步为指向 execution。
- **行为保持:** 本任务不改任何方法的执行语义、产物、ledger/trace 形态,纯收敛。

## 非目标

- 不强行收敛**有真实多分支语义**的路由器(例如 `gene_annotation`
  `route_after_evm_freeze`/`route_after_busco_*`、各 stage 的 candidate/merge/review
  gate 路由、`read_qc` `route_after_make_fofn`/`length_filter`)。只收敛"failed 则
  fail,否则固定下一步"的薄转发器;其余保留显式,避免过度抽象。
- 不新增任何 config / descriptor / YAML 面;`{suffix: cfg}` 是 stage 内部 Python
  常量,不是新的外部配置面。
- 不改图的节点名 / 边 / gate 注册;不顺手改相邻逻辑。
- 不在本任务内落地任何新方法的真实执行(那是 Phase 7 执行落地线的事)。

## 任务

- [x] **切片 1:** `build_frozen_attempt` + `run_frozen_attempt` 实现于
  `app/execution/method_runner.py`,逐 stage 迁移并按 stage 跑测试,零行为差异。
- [x] **切片 2:** gene_annotation 用 `functools.partial(_route_after_freeze/_run, suffix=...)`
  替换 4 个薄转发器。其余 stage 当前单方法,无去重收益,推迟到新增第二方法时再做。
- [x] **`gene_annotation` 对齐:** 私有 `_freeze/_run_gene_method_attempt` 与 evm/busco
  freeze/run 均改用共享 helper;miniprot/augustus 薄转发器删除。
- [x] **architecture.md 同步:** forge 段落(architecture.md 模块表/原则 5、
  governance.md 脚本生成节)已改为指向 `execution/method_runner`。
- [x] **回归:** `pytest -q` 613 passed,仅既有 `AssemblyRerunTests` 的 7 个 Flye
  环境失败(与本重构无关),无新增回归。

## 验收

- 各 method-bearing stage 的 `freeze/run/collect` 节点与薄路由转发器由共享 helper +
  `{suffix: cfg}` 表驱动;新增一个方法只需追加一行 cfg + 脚本正文,不再复制节点/路由
  样板。
- 多分支路由器(EVM/BUSCO/candidate/merge/gate 等)保持显式,未被错误并入表。
- 图结构(节点名、边、gate)与所有方法的执行语义、产物、ledger/trace 形态完全不变。
- 共享 helper 落点已定且 `architecture.md`/`governance.md` 与代码一致。
- 全量测试通过,无新增回归。
