# Gene Agent

Gene Agent 是一个面向长读长基因组组装与注释项目的 LangGraph 工作流
Agent。它把传统生信 pipeline 中分散的脚本、参数、人工判断和运行记录，
整理成可恢复、可审计、可人机协同决策的工程化流程。

一句话概括：

> 从测序数据或已有组装结果出发，Gene Agent 负责识别输入、规划分析阶段、
> 调度已批准的本地生信工具、记录产物与证据，并在需要人工判断的位置停下来。

## Why This Project Exists

长读长基因组分析通常不是一次命令就能结束的任务。真实项目里会遇到：

- 输入文件类型复杂：ONT、HiFi、CLR、CycloneSEQ long reads、short reads、Hi-C、已有 assembly、注释证据等。
- 分析阶段长：read QC、genome survey、assembly、assembly review、Hi-C scaffolding、repeat/gene/function annotation。
- 方法选择依赖上下文：同一阶段可能有 Flye、NextDenovo、QUAST、BUSCO、RepeatMasker、Miniprot、AUGUSTUS、DIAMOND 等不同工具。
- 中间结果巨大：FASTQ、FASTA、BAM、GFF3、日志和指标表不适合塞进 agent state。
- 生物学判断不能乱自动化：assembly 是否接受、Hi-C scaffolding 是否可信、gene set 是否进入下游，都需要证据和人工 gate。

Gene Agent 的目标不是“替代所有生信专家”，而是把一个多阶段 genome project
变成一个有状态、有证据、有恢复能力的工作流系统。

## Workflow

```text
inventory
-> read_qc
-> genome_survey
-> assembly
-> assembly_dedup
-> assembly_review
-> hic_scaffolding
-> t2t
-> repeat_annotation
-> gene_annotation
-> function_annotation
-> done
```

核心原则是保持 top-level state 很小：graph state 只保存 session、当前阶段、
artifact ref、route trace、interrupt 信息等控制字段；大文件、日志、指标表和
脚本都落在 artifact registry、ledger 或运行目录里。

## What It Can Do Today

已实现的核心能力：

- **LangGraph stage orchestration**：thin parent graph 负责路由，每个生信阶段封装成独立 subgraph。
- **Conversational workflow entry**：正常用户从 `gene-agent tui` 描述目标、补齐样本事实、确认运行输入并启动或恢复工作流。
- **Opt-in local execution**：只有显式批准的 stage 才会真正运行本地工具；未批准或未满足条件时输出计划、blocked state 或 review gate。
- **Shared execution boundary**：通过 `EnvResolver`、`env_dispatch`、`LocalRunner` 统一解析 conda 环境、做 preflight、冻结命令并收集产物。
- **Durable resume**：基于 sqlite checkpointer、ledger 和 decision file，支持跨进程恢复 `assembly_review`、`hic_scaffolding` 等人工 gate。
- **Human-in-the-loop review**：review registry 统一声明 gate、允许的决策、resume applier 和 prompt bundle，避免每个阶段各写一套暂停/恢复逻辑。
- **Operator observability**：`inspect`、`review`、run trace、operator report 能从 durable records 里还原 session 状态、下一步动作和关键 artifact。

当前 descriptor registry 中的全部方法都具有可执行路径：

| Stage | Registered executable methods |
|---|---|
| inventory | —（注册 manifest assets，不选择工具） |
| read_qc | SeqKit stats、显式 skip、SeqKit length filter |
| genome_survey | Jellyfish、GenomeScope2 |
| assembly | Flye、Hifiasm、Hifiasm-ONT、NextDenovo |
| assembly_dedup | purge_dups |
| assembly_review | QUAST、BUSCO |
| hic_scaffolding | AutoHiC（内部使用 Juicer、3D-DNA 和模型校正） |
| t2t | minimap2 |
| repeat_annotation | RepeatMasker |
| gene_annotation | AUGUSTUS、Miniprot |
| function_annotation | DIAMOND blastp |

Gene Annotation 还会运行不可由操作员选择的内部 helper：gffread、EVM 和
BUSCO；它们不属于 stage 的 `by_stage` 方法列表。

未实现的方法不进入 registry，也不出现在当前能力表中。

## Architecture

```text
app/
  workflow.py   canonical stage order, navigation, and terminal failure routing
  domains/      stage-neutral biological rules and shared input parsing
  graph/        parent LangGraph, route controller, checkpoint factory
  stages/       one subgraph per biological workflow stage
  schemas/      typed contracts for state, assets, plans, descriptors, artifacts
  execution/    env resolution, frozen attempts, local runner, process evidence
  storage/      run layout, artifact publication/index, parameter evidence, ledger
  review/       review gate registry, policies, recommendation/resume helpers
  control/      lifecycle and resume semantics
  reports/      operator reports and run traces
  entrypoints/  CLI boundaries, planner/TUI, review/resume commands

docs/
  architecture.md          system architecture and ownership boundaries
  architecture/            current cross-cutting architecture specifications
    resource-handoff.md     cross-stage logical resource contract
    data-v2.md              run data and parameter-recommendation persistence contract
  stage_contracts.md        current stage inputs, outputs, methods, and review triggers
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

正常用户只需要启动 TUI。TUI 收集本次样本事实、确认 manifest 和 run config，
冻结输入后调用内部执行器；各 stage 再按已确认的输入和目标决定执行、跳过或
进入人工 gate。`bootstrap` 只是初始化或修复本机配置的辅助工具。

`bootstrap --apply` 在写入 `config/envs.yaml`、完成 Juicer conformance 后，
还会在当前主机逐项检查配置中的全部 method。检查使用实际 method -> stage ->
default 解析出的 conda 环境执行 `conda run ... command -v`，并按相同环境去重。
AutoHiC、Juicer、3D-DNA、juicer-tools 的安装目录、canary 和模型路径仍由 package
preflight 检查。任何失败都会列出具体 method、conda target、缺失工具或路径以及
修复提示；修好后重新运行 bootstrap 即可。

### 1. TUI / Plan Layer: choose stages

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
对 /data/project/reads.fastq.gz 以卵鳎为目标物种进行 chromosome 级别基因组组装和注释
```

TUI 的实际流程是：

1. 让用户用自然语言描述任务。
2. 解析这句指令，生成内存中的 planner state，并 materialize manifest。
3. 进入 `Plan Draft` 页面，展示 session、data dir、manifest、inputs、stages、methods、parameters 和 attention 项。
4. 在 draft 里可以按 `Enter/c` 继续，按 stage 编号编辑单个 stage，按 `s` 编辑 stage list，按 `r` 重新手动规划。
5. 进入单个 stage 后，可以按 `m` 改 method，按 `p` 改 parameters，按 `b` 返回 draft。
6. draft 锁定后不会立刻执行；TUI 会再展示 final run plan 并询问 `Proceed?`。
7. 用户确认后，TUI 写出 run config，把选中的 stage 写成 `requested_stages`，把可执行 stage 写成 `local_exec`。
8. TUI 冻结输入后，在同一进程内调用 session executor；不会再拼装或启动另一个 CLI 命令。
9. 如果运行中遇到 human gate，TUI 会启用 interactive gate loop，在 sqlite checkpoint 上收集决策、写 decision artifact、更新 checkpoint 并继续 graph。

Plan 层控制的是“这次项目要走哪些 stage”。例如只想从已有 assembly 开始做
注释，可以只选择：

```text
assembly_review -> repeat_annotation -> gene_annotation -> function_annotation
```

如果要从 raw long reads 做完整主线，可以选择：

```text
read_qc -> genome_survey -> assembly -> assembly_dedup -> assembly_review
-> hic_scaffolding -> t2t -> repeat_annotation -> gene_annotation
-> function_annotation
```

TUI 最终会把选择落到 run config：

```json
{
  "requested_stages": [
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation"
  ]
}
```

`requested_stages` 决定 parent graph 需要推进哪些业务阶段；未请求的阶段由路由
和 stage contract 跳过或不进入。

### 2. RunConfig Layer: choose execution and parameters

`requested_stages` 只说明“要走哪些阶段”。是否真的运行本地工具，以及每个工具
的参数，由 `local_exec` 和各 stage 的 config 控制。

示例：从 long reads 做 read QC、Flye assembly、QUAST/BUSCO review，但暂时不
执行 annotation 工具：

```json
{
  "requested_stages": [
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation"
  ],
  "local_exec": [
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review"
  ],
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

`local_exec` 是安全阀：只有列在这里的 stage 才会尝试执行本地工具。没有列入
`local_exec` 的 stage 仍然可以参与规划、写 artifact、进入 review gate 或作为
下游 contract 继续传递，但不会偷偷运行昂贵命令。

`run` 和 `ask` 兼容命令已移除。新运行只有一个入口：
`gene-agent tui`。TUI 负责收集输入、确认计划、冻结 manifest/run config，并调用
内部 session executor；自动化不应绕过这条冻结边界。

### 3. Stage selection and gates

即使 `requested_stages` 里包含某个 stage，stage 内部仍会根据 manifest、
artifact registry、目标级别和已有产物决定执行、跳过或阻塞。

几个实际例子：

- **existing assembly input**：如果 manifest 只提供已有 assembly，inventory 会
  直接把主线推进到 `assembly_review`，并记录 `read_qc`、`genome_survey`、
  `assembly` 的 skip reason。
- **Hi-C scaffolding**：父图只在 `requested_stages` 选择了
  `hic_scaffolding` 时进入该子图；未选择时直接路由到下一个请求的 stage。
  一旦选择，stage 会检查 contig assembly、成对 Hi-C R1/R2 reads 和 AutoHiC
  运行配置，缺少必要条件会以 `blocked_missing_requirements` 终止，而不会写
  stage-local skip。AutoHiC 是固定且唯一的方法；LLM 推荐限制酶并等待人工
  批准后，AutoHiC 内部完成 Juicer mapping、3D-DNA scaffolding、深度学习
  纠错、染色体划分和 HTML report 生成，最后进入染色体组装采纳 gate。
- **human gates**：`assembly_review`、`hic_scaffolding`、`t2t`、`gene_annotation`
  等阶段会在需要人工判断时进入 `blocked_human`，并写 review request artifact。
- **provenance gates**：`function_annotation` 需要明确的 database provenance；
  没有数据库来源时不会假装成功。

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
`requested_stages` 绕过它。用户明确选择 Hi-C 后，stage 就按 AutoHiC 流程执行。

## Inspect / Review / Resume

Inspect a durable session:

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /absolute/path/to/data
```

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

## Adding an Executable Method

The invariant is **registered = executable**. A new method is added as one
complete slice:

1. add or reuse its package descriptor and add its method descriptor;
2. register the package, method, and stage entry in
   `descriptors/registry.yaml`;
3. implement a stage adapter that prepares `FrozenAttemptSpec` values and
   collects biological outputs through the shared execution lifecycle;
4. add stage routing and run-config validation for its actual parameters;
5. expose the canonical method ID to the planner where user selection applies;
6. add selection, execution, failure, and artifact tests.

`tests/test_registry_executable_invariant.py` rejects a registered method that
does not have a wired execution path.

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
- Function annotation requires explicit database provenance before terminal completion.
- Large biological files stay outside graph state.

## Read More

- `docs/architecture.md`：系统架构和模块边界
- `docs/architecture/README.md`：当前规范注册表与职责边界
- `docs/architecture/resource-handoff.md`：跨阶段逻辑资源交接
- `docs/architecture/execution-lifecycle.md`：可执行方法的统一 freeze/run/collect 生命周期
- `docs/architecture/data-v2.md`：新 session 的数据、参数推荐、审计、恢复与保留规范
- `docs/stage_contracts.md`：各阶段输入/输出合同
- `docs/deployment/migration.md`：新机器安装与验证
- `docs/deployment/mac_dev_server_pull.md`：Mac 开发与服务器/HPC 执行边界
