# Gene Agent

Gene Agent 是一个面向长读长基因组组装项目的 LangGraph 工作流
Agent。它把传统生信 pipeline 中分散的脚本、参数、人工判断和运行记录，
整理成可恢复、可审计、可人机协同决策的工程化流程。

一句话概括：

> 从测序数据或已有组装结果出发，Gene Agent 负责识别输入、规划分析阶段、
> 调度已批准的本地生信工具、记录产物与证据，并在需要人工判断的位置停下来。

## Why This Project Exists

长读长基因组分析通常不是一次命令就能结束的任务。真实项目里会遇到：

- 输入文件类型复杂：ONT、HiFi、CLR、CycloneSEQ long reads、short reads、Hi-C、已有 assembly 等。
- 分析阶段长：read QC、genome survey、assembly、assembly review、Hi-C scaffolding、T2T finishing。
- 方法选择依赖上下文：同一阶段可能有 Flye、NextDenovo、QUAST、BUSCO、AutoHiC、minimap2 等不同工具。
- 中间结果巨大：FASTQ、FASTA、BAM、日志和指标表不适合塞进 agent state。
- 生物学判断不能乱自动化：assembly 是否接受、Hi-C scaffolding 是否可信、T2T patch 是否采用，都需要证据和人工 gate。

Gene Agent 的目标不是“替代所有生信专家”，而是把一个多阶段 genome project
变成一个有状态、有证据、有恢复能力的工作流系统。

## Stage Registry And Planning Recommendations

```text
inventory
-> read_qc
-> genome_survey
-> assembly
-> assembly_dedup
-> assembly_review
-> hic_scaffolding
-> gapcloser
-> t2t
-> base_polish
-> done
```

This is the unique stage registry/display order, not an automatic planning
template or runtime successor table. It includes the manual-only `gapcloser`
and `base_polish` placeholders. Runtime order comes only from the literal
frozen `requested_stages` occurrence list. The automatic Assembly template
appends `assembly_review`; the T2T template omits the placeholders and repeats
that same Review component after `t2t`:

```text
read_qc -> genome_survey -> assembly -> assembly_dedup -> assembly_review
-> hic_scaffolding -> t2t -> assembly_review
```

The parent graph is composable but statically registered in `app/graph/builder.py`;
it does not dynamically discover stage plugins or insert successor stages.
Before freeze, sequence validation rejects a plan that leaves an Assembly or T2T
boundary without its required `assembly_review`; it never inserts one. A later
assembly-refining occurrence may become the exact handoff that Review closes.
The owner contracts are [runtime orchestration](docs/architecture/topics/runtime-orchestration.md)
and [stage contracts](docs/architecture/reference/stage-contracts.md).

`gapcloser` 和 `base_polish` 当前只是父图中已注册的 manual-only
placeholder：自动规划不会加入它们；显式请求会以
`blocked_not_implemented` fail closed，不会伪装成已经执行。

核心原则是保持 top-level state 很小：graph state 只保存 session、当前阶段、
artifact ref、route trace、interrupt 信息等控制字段；大文件、日志、指标表和
脚本都落在 artifact registry、ledger 或运行目录里。

## What It Can Do Today

已实现的核心能力：

- **LangGraph stage orchestration**：thin parent graph 负责路由，每个生信阶段封装成独立 subgraph。
- **Deterministic workflow entry**：操作员可用结构化 manifest 和 run config 通过 `gene-agent run` 启动可复现运行；TUI 仍提供可选的对话式 intake 与规划。
- **Verified frozen-input consumption**：executor 在建图前验证完整冻结事务；Inventory 只消费该 in-memory verified bundle，trace 也不会信任 hash-invalid 的 run config。
- **Opt-in local execution**：只有显式批准的 occurrence 才会真正运行本地工具；同一 stage 的重复 occurrence 可分别授权或拒绝。未批准或未满足条件时输出计划、blocked state 或 review gate。
- **Shared execution boundary**：通过 `EnvResolver`、`env_dispatch`、`LocalRunner` 统一解析 conda 环境、做 preflight、冻结命令并收集产物。
- **Durable resume**：基于 sqlite checkpointer、ledger 和 decision file，支持跨进程恢复 `assembly_review`、`hic_scaffolding` 等人工 gate。
- **Human-in-the-loop review**：review registry 统一声明 gate、允许的决策、occurrence 校验、dispatch 与 timing metadata；stage code 仍拥有 payload 语义、prompt 分支和 resume effects。
- **Reusable assembly review**：每个 Review occurrence 都冻结一个精确的 assembly producer target，并验证 central `ResourceRef` provenance 后才加载 candidate；后续 Review 复用同一合同。
- **Operator observability**：`inspect` 和 session run trace 从 durable records
  还原状态、下一步动作和关键 artifact；blocked/failed operator report 是运行边界
  写下的 live compact projection，当前重建限制见 architecture conformance。

当前 descriptor registry 中的全部方法都具有已接线的运行或规划语义：

| Stage | Registered capabilities |
|---|---|
| inventory | —（注册 manifest assets，不选择工具） |
| read_qc | SeqKit stats、显式 planner-only skip、SeqKit length filter |
| genome_survey | Jellyfish、GenomeScope2 |
| assembly | Flye、Hifiasm、Hifiasm-ONT、NextDenovo |
| assembly_dedup | purge_dups |
| assembly_review | QUAST、BUSCO |
| hic_scaffolding | AutoHiC（内部使用 Juicer、3D-DNA 和模型校正） |
| t2t | minimap2 |

`gapcloser` 和 `base_polish` 不在 descriptor registry 中，也不允许写入
`allow_local_execution: true`。它们目前只有父图位置、严格状态合同和显式失败边界，没有可执行方法。

未实现的方法不进入 registry，也不出现在当前能力表中。

## Architecture

```text
app/
  workflow.py   stage registry order, default planning, and failure predicates
  control/      frozen occurrence plan plus lifecycle and resume semantics
  domains/      stage-neutral biological rules and shared input parsing
  graph/        parent LangGraph, route controller, checkpoint factory
  stages/       one subgraph per biological workflow stage
  schemas/      typed contracts for state, assets, plans, descriptors, artifacts
  execution/    env resolution, frozen attempts, local runner, process evidence
  storage/      run layout, artifact publication/index, parameter evidence, ledger
  review/       review gate registry, policies, recommendation/resume helpers
  reports/      operator reports and run traces
  entrypoints/  CLI boundaries, planner/TUI, review/resume commands

docs/
  architecture/
    README.md               topic router and architecture graph
    topics/                 system map and task-oriented subsystem design
    reference/              exact contracts, governance, and conformance debt
  deployment/               installation and server-execution guidance
  user_guide/               current operator guides
```

The parent graph intentionally stays thin:

```text
START -> route_controller -> <stage_subgraph> -> route_controller -> END
```

Stage subgraphs own method planning, validation, execution handoff, artifact
collection and review boundaries. This keeps biological method choice out of
the parent router and makes each stage independently testable.

## How To Run It

真实/操作员运行从确定性的结构化输入边界开始：

```text
gene-agent run --manifest PATH --run-config PATH --data-dir PATH
```

`manifest` 是唯一的 `session_id` 所有者；`data-dir` 是布局根目录，运行输出固定在
`<data-dir>/runs/<session_id>/`。命令在写出任何运行输入之前校验并规范化两个文件，
要求 run config 显式提供 `requested_stages` JSON 对象列表；每项形如
`{"stage": "assembly_review", "allow_local_execution": true}`，其中 stage
定义一个有序 occurrence，布尔值只授权该 occurrence 启动本地进程。列表保留字面
顺序、允许重复且无需遵循 registry order，但必须通过 RunConfig 序列校验；
冻结时会在首位隐式加入 `step-000-inventory`，其余 `step_id`
由列表位置生成。`requested_stages` 不得为 `null`，但 `[]` 是有效的 inventory-only
计划。随后命令冻结 occurrence 序列及保守的依赖闭包，并以 SQLite 执行已冻结的
会话。冻结发布顺序为
`input_manifest.yaml`、`run_config.json`、`dependency_manifest.json`，最后才是提交标记
`frozen_inputs.json`。

例如，复制并填写 `docs/examples/` 的模板后：

```bash
gene-agent run \
  --manifest /absolute/path/to/manifest.yaml \
  --run-config /absolute/path/to/run_config.json \
  --data-dir /absolute/path/to/data
```

新的结构化运行要求 stdin/stdout 都连接真实 TTY；校验失败会在 freeze 前退出，不会
发布 frozen inputs。公共 `run` 不提供非交互新运行分支。到达 gate 后，当前终端负责
倒计时、人工决定和继续执行；若进程中断，durable `blocked_human` checkpoint 仍可通过
`inspect`、`review` 和 `resume` 恢复。

`gene-agent tui` 是可选的对话式 intake/规划入口：它收集样本事实、生成可编辑 Plan
Draft，并在确认后进入同一冻结和执行边界。未回答 stage selection 是 `None`；明确选择
Inventory-only 是 `[]`。当推荐为空时，stage prompt 的 Enter 明确接受这个
Inventory-only 推荐，跳过 occurrence-local consent、method 与 parameter 问题，随后进入
final confirmation。`bootstrap` 只是初始化或修复本机配置的辅助工具。

`bootstrap --apply` 在写入 `config/envs.yaml`、完成 Juicer conformance 后，
还会在当前主机逐项检查配置中的全部 method。检查使用实际 method -> stage ->
default 解析出的 conda 环境执行 `conda run ... command -v`，并按相同环境去重。
AutoHiC、Juicer、3D-DNA、juicer-tools 的安装目录、canary 和模型路径仍由 package
preflight 检查。任何失败都会列出具体 method、conda target、缺失工具或路径以及
修复提示；修好后重新运行 bootstrap 即可。

### 1. Optional TUI / Plan Layer: choose stages

TUI 的入口就是启动一个交互式对话框：

```bash
gene-agent tui
```

启动后会出现类似提示：

```text
=== gene-agent interactive ===

Describe what you want to do (e.g. '对 /path/reads.fa 以卵鳎为目标物种进行 T2T 级别基因组组装'):
>
```

这时输入一句话任务，例如：

```text
对 /data/project/reads.fastq.gz 以卵鳎为目标物种进行 chromosome 级别基因组组装
```

TUI 的实际流程是：

1. 让用户用自然语言描述任务。
2. 用 LLM intent parser 和保守的本地识别器确定 intake 追问范围；这一步只决定还要问哪些样本事实，不决定最终 stages。
3. 逐项确认当前 intake 支持的 HiFi、ONT、成对 Hi-C、物种和 telomere motif，并持续重建 `manifest_draft.yaml`。
4. intake 完成后，把原始请求、确认后的 manifest 和现有 method catalog 交给 LLM，生成一个复用 `AskPlanState` 字段的 Plan Draft：有序 `{stage, allow_local_execution}` occurrence、methods 和部分 planning-time parameters。
5. 进入现有 `Plan Draft` 页面，展示 session、data dir、manifest、inputs、occurrences、methods、parameters 和 attention 项。
6. 在 draft 里可以按 `Enter/c` 继续，按 occurrence 编号编辑单项，按 `s` 编辑可重复的有序 occurrence list，按 `r` 丢弃当前 draft；丢弃后可通过 `/resume` 重新生成。
7. 进入单个 occurrence 后，可以按 `l` 单独授权/拒绝本地执行，按 `m` 改 stage-scoped method，按 `p` 改 parameters，按 `b` 返回 draft。依赖上游结果的运行期推荐参数仍显示为 deferred，不在这里伪造数值。
8. draft 锁定后不会立刻执行；TUI 会再展示 final run plan 并询问 `Proceed?`。拒绝时返回同一个 Plan Draft 继续编辑。
9. 用户确认后，TUI 原样写出 occurrence 对象；每个布尔值只授权对应 occurrence 是否可在本机启动工具。
10. TUI 冻结输入后，在同一进程内调用 session executor；不会再拼装或启动另一个 CLI 命令。运行中遇到 human gate 时，TUI 在 sqlite checkpoint 上收集决策、写 decision artifact、更新 checkpoint 并继续 graph。

当前 TUI intake 的输入范围是 PacBio HiFi、ONT 和显式成对的 Hi-C reads，外加
物种及 telomere motif。CLR、CycloneSEQ、short reads 和已有 assembly 尚未接入
这段追问流程。规划 LLM 失败或返回非法 draft 时，本次规划中止并保留草稿供
`/resume` 重试，不使用确定性 stage plan 回退。

TUI 最终会把选择落到 run config：

```json
{
  "requested_stages": [
    {"stage": "read_qc", "allow_local_execution": true},
    {"stage": "genome_survey", "allow_local_execution": true},
    {"stage": "assembly", "allow_local_execution": true},
    {"stage": "assembly_dedup", "allow_local_execution": true},
    {"stage": "assembly_review", "allow_local_execution": true}
  ]
}
```

`requested_stages` 的字面顺序就是业务 occurrence 顺序；未列出的 stage 没有运行时
occurrence，也不会伪造 skipped 事件。每个 item 的 `allow_local_execution` 只决定
该 occurrence 能否启动本地进程。对公共 `gene-agent run`，它必须是显式、非
`null` 的 JSON 对象列表；`None` 只表示 planner/TUI 尚未完成的草稿，不能进入冻结
或运行边界。

### 2. RunConfig Layer: choose execution and parameters

`requested_stages` 的每个对象同时说明“要走哪个阶段”和“该阶段是否授权本地执行”。
方法参数仍由各 stage 的 config 控制。

示例：从 long reads 做 read QC、Flye assembly、purge_dups 和 QUAST/BUSCO
review：

```json
{
  "requested_stages": [
    {"stage": "read_qc", "allow_local_execution": true},
    {"stage": "genome_survey", "allow_local_execution": true},
    {"stage": "assembly", "allow_local_execution": true},
    {"stage": "assembly_dedup", "allow_local_execution": true},
    {"stage": "assembly_review", "allow_local_execution": true}
  ],
  "review_policy": {
    "max_blocked_seconds": 300,
    "stages": {
      "assembly_review": {
        "max_blocked_seconds": -1
      }
    }
  },
  "shared_parameters": {
    "platform": "ont",
    "read_type": "ont_raw"
  },
  "read_qc": {
    "length_filter": {
      "method": "read_qc.length_filter.seqkit_seq",
      "approved": true,
      "thresholds": {
        "min_len": 1000
      }
    }
  },
  "genome_survey": {
    "survey_parameters": {
      "kmer_size": 21,
      "hash_size": "1G",
      "threads": 8
    }
  },
  "assembly": {
    "assembly_method": "flye",
    "assembly_route": "ont",
    "flye_parameters": {
      "threads": 16
    }
  },
  "assembly_review": {
    "quast": {
      "threads": 8
    },
    "busco": {
      "lineage_path": "/absolute/path/to/busco/lineage",
      "lineage_name": "insecta_odb10",
      "lineage_version": "odb10",
      "lineage_source": "local",
      "threads": 8
    }
  }
}
```

用户提供的 genome size 属于输入样本事实，只写在 manifest 的
`genome_size_estimate`；RunConfig 不重复保存该值。运行中由 genome survey
得到的测量值则保留为独立的 stage output。

`allow_local_execution` 是 occurrence 级本地执行授权边界：只有为 `true` 的
requested-stage 对象才可启动本地工具，`assembly_dedup` 也遵守同一规则。同名
stage 的两个 occurrence 可以分别授权或拒绝本地执行。对象的 stage 仍可被
请求而其布尔值为 `false`；此时它不得运行本地命令，并由该 stage 的运行契约决定
如何验证输入和处理该 occurrence。

`review_policy.max_blocked_seconds` 的默认值是 `300`。可在
`review_policy.stages.<stage>.max_blocked_seconds` 为已注册的 review stage 覆盖；
未注册 stage 会被拒绝。`-1` 表示无限等待人工决定；`0` 表示在前台交互运行中立即
触发该 gate 已有且仍满足资格的自动 resolver；正整数则给出按 Enter 可接管的倒计时，
并在到期时触发同一 resolver。timer 本身不提供决定，也不会从 registry 默认项伪造
批准。当前 Assembly Review 保留原有的 evidence eligibility + LLM recommendation
链，assembly route selection 保留原有的推荐路线 timeout applier；Assembly、AutoHiC
和 T2T 的 parameter approval 共用一个 resolver，只能接受 immutable request 绑定的
当前持久化推荐。没有 resolver 的 gate 仍直接进入人工处理。自动 transaction 失败时，
只有同一个 blocked request 仍可验证，当前终端才回落人工提示；身份或完整性漂移会
fail closed。倒计时只存在于由 `run` 或 TUI 启动的原始 attached executor 中：按 Enter
会取消倒计时并打开人工决策提示；Ctrl-C、EOF 或进程退出只留下 durable
`blocked_human` checkpoint。之后必须用 `review` 或带 decision file 的 `resume` 人工
处理；重新执行同一 frozen session 也只显示这些人工恢复命令，不调用 blocked graph、
不按旧的 `blocked_at` 或已流逝的时间自动采纳，也没有 daemon/background wake。公共
`resume` 没有 review timeout 覆盖，且不存在 strict/auto review mode。

`run` 是公开的确定性新运行边界；它不接受 `session-id`、checkpointer 或
`local-exec` 覆盖参数。TUI 负责可选的对话式输入、确认计划和冻结 handoff；两条路径
都只通过同一 frozen-session executor 运行。

### 3. Stage selection and gates

即使 `requested_stages` 里包含某个 stage，stage 内部仍会根据 manifest、
artifact registry、目标级别和已有产物决定执行、跳过或阻塞。

几个实际例子：

- **existing assembly input**：如果 manifest 提供已有 assembly，Inventory 将它注册为
  central `assembly` resource。`assembly_review` 是通常推荐的首个分析 occurrence，
  不是公共 `gene-agent run` 的冻结前限制；公共 run 保留操作者提供的 occurrence
  顺序。每个实际到达的 stage 自行校验其输入，未列 stage 没有 occurrence，也不会生成
  synthetic skip。
- **Hi-C scaffolding**：父图只在当前冻结 occurrence 是 `hic_scaffolding` 时进入
  该子图；未列入 `requested_stages` 就没有这个 occurrence，也不产生 bypass/skip
  事件。一旦到达，stage 会检查 contig assembly、成对 Hi-C R1/R2 reads 和 AutoHiC
  运行配置，缺少必要条件会以 `blocked_missing_requirements` 终止，而不会写
  stage-local skip。AutoHiC 是固定且唯一的方法；LLM 推荐限制酶并等待人工
  批准后，AutoHiC 内部完成 Juicer mapping、3D-DNA scaffolding、深度学习
  纠错、染色体划分和 HTML report 生成，最后进入染色体组装采纳 gate。
- **human gates**：`assembly_review`、`hic_scaffolding`、`t2t` 等阶段会在需要
  人工判断时进入 `blocked_human`，并写 review request artifact。

manifest 中的目标级别会影响 stage 内部决策：

```yaml
session_id: demo-session
sample_id: demo
target_assembly_level: chromosome
genome_size_estimate: 500000000
assets:
  - asset_type: raw_long_reads
    paths:
      - /absolute/path/to/reads.fastq.gz
    platform: ont
    format: fastq
  - asset_type: hic_reads_registered
    paths:
      - /absolute/path/to/hic_R1.fastq.gz
      - /absolute/path/to/hic_R2.fastq.gz
    platform: illumina
    layout: paired
    format: fastq
```

如果目标仅为 contig level，规划时不应选择 `hic_scaffolding`；父图会按
冻结的 `requested_stages` 根本不创建该 occurrence。用户明确选择 Hi-C 后，只有
对应 occurrence 的 `allow_local_execution: true` 才允许按 AutoHiC 流程启动本地
工具。

## Inspect / Review / Resume

Inspect a durable session:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data
```

外部同时启动多个 contig 时，每个 contig 仍是一份 manifest、RunConfig 和唯一
`session_id`，分别调用同一个 `run`。聚合方只轮询各 session 的只读 JSON：

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data \
  --json
```

`inspect_schema_version: 1` 提供 occurrence 级 `workflow_progress` 和 `execution`；
它们直接来自 verified frozen plan、Ledger、checkpoint 与本机 execution lock，不会
写回批次状态或进度投影。`inspect --trace` 会生成 report，不能作为轮询协议。

Review and resume a blocked human gate:

```bash
gene-agent review \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data \
  --checkpointer sqlite
```

Generate a decision template for automation:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data \
  --write-decision-template /absolute/path/to/decision.json

gene-agent resume \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data \
  --decision-file /absolute/path/to/decision.json \
  --checkpointer sqlite
```

## Adding a Registered Method Capability

The invariant is **registered = wired behavior**: a local method needs a real
execution adapter, while a `planner_only` method needs explicit, tested routing
semantics and must not pretend to launch a process. A new method is added as one
complete slice:

1. add or reuse its package descriptor and add its method descriptor;
2. register the package, method, and stage entry in
   `descriptors/registry.yaml`;
3. for `local` execution, implement a stage adapter that prepares
   `FrozenAttemptSpec` values and collects biological outputs through the shared
   execution lifecycle; for `planner_only`, implement the explicit no-process
   stage behavior;
4. add stage routing and run-config validation for its actual parameters;
5. expose the canonical method ID to the planner where user selection applies;
6. add selection, execution, failure, and artifact tests.

`tests/test_registry_executable_invariant.py` derives registered local coverage
from imported lifecycles, verifies each shared freeze/run/collect graph and
outer stage node, and proves the planner-only Read-QC skip route cannot launch
a filter attempt. Descriptor/configuration tests also fail closed on undeclared
descriptor YAML fields.

## Verification

Primary regression suite:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache \
conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
```

Compile check:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache \
conda run -n gene_agent python -m compileall app tests
```

Graph smoke:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache \
conda run -n gene_agent python -c "from app.graph.builder import build_graph; from app.schemas.state import initial_state; g=build_graph(); s=initial_state(session_id='smoke', input_manifest_ref='inputs/manifest.yaml'); r=g.invoke(s, config={'configurable': {'thread_id': 'smoke'}}); print(r['current_stage']); print(r['route_trace'])"
```

## Current Boundaries

Intentional boundaries:

- No in-agent remote execution. Real heavy bioinformatics runs happen by running the same CLI inside the server/HPC checkout.
- Real execution is opt-in per stage. The agent should not silently launch expensive tools.
- Unimplemented methods remain outside the descriptor registry.
- Large biological files stay outside graph state.

## Read More

- `docs/architecture/README.md`：按问题选择主题、沿依赖边按需加载设计
- `docs/architecture/topics/system-map.md`：系统地图与两级冻结边界
- `docs/architecture/topics/tui-and-planning.md`：TUI、规划和整次运行输入冻结
- `docs/architecture/topics/execution.md`：方法级 freeze/run/collect 生命周期
- `docs/architecture/topics/data-and-storage.md`：状态、资源、artifact、ledger 和 checkpoint 的数据流
- `docs/architecture/reference/stage-contracts.md`：各阶段输入/输出合同
- `docs/architecture/reference/conformance.md`：已接受设计与当前实现之间的明确差距
- `docs/deployment/migration.md`：新机器安装与验证
- `docs/deployment/mac_dev_server_pull.md`：Mac 开发与服务器/HPC 执行边界
