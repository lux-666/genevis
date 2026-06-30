# Gene Agent

Gene Agent 是一个面向长读长基因组组装与注释项目的 LangGraph 工作流
Agent。它把传统生信 pipeline 中分散的脚本、参数、人工判断和运行记录，
整理成可恢复、可审计、可人机协同决策的工程化流程。

一句话概括：

> 从测序数据或已有组装结果出发，Gene Agent 负责识别输入、规划分析阶段、
> 调度已批准的本地生信工具、记录产物与证据，并在需要人工判断的位置停下来。

## Why This Project Exists

长读长基因组分析通常不是一次命令就能结束的任务。真实项目里会遇到：

- 输入文件类型复杂：ONT/CLR long reads、short reads、Hi-C、已有 assembly、注释证据等。
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
- **Manifest / natural-language entry**：支持 `gene-agent run --manifest ...` 结构化入口，也支持 `gene-agent ask "..."` 生成 ask plan、补充澄清信息并落到同一条 run 路径。
- **Opt-in local execution**：只有显式批准的 stage 才会真正运行本地工具；未批准或未满足条件时输出计划、blocked state 或 review gate。
- **Shared execution boundary**：通过 `EnvResolver`、`env_dispatch`、`LocalRunner` 统一解析 conda 环境、做 preflight、冻结命令并收集产物。
- **Durable resume**：基于 sqlite checkpointer、ledger 和 decision file，支持跨进程恢复 `assembly_review`、`hic_scaffolding` 等人工 gate。
- **Human-in-the-loop review**：review registry 统一声明 gate、允许的决策、resume applier 和 prompt bundle，避免每个阶段各写一套暂停/恢复逻辑。
- **Operator observability**：`inspect`、`review`、run trace、operator report 能从 durable records 里还原 session 状态、下一步动作和关键 artifact。

已接入或规划的生信工具状态：

| Stage | Executable slices | Planning-only / pending slices |
|---|---|---|
| read_qc | SeqKit stats, optional length filter | broader QC reports |
| genome_survey | Jellyfish, GenomeScope2 | additional survey methods |
| assembly | Flye, NextDenovo | Canu, hifiasm |
| assembly_review | QUAST, BUSCO evidence | Merqury, coverage/contamination checks |
| assembly_dedup | purge_dups-style candidate handoff | broader purge/polish loops |
| hic_scaffolding | Juicer + 3D-DNA, YaHS | raw Hi-C BAM generation gaps |
| t2t | minimap2 evidence, conservative telomere patch handoff | broader gap-closing strategies |
| repeat_annotation | RepeatMasker with provided libraries | EDTA, RepeatModeler, TRF |
| gene_annotation | Miniprot, AUGUSTUS, EVM merge, BUSCO signal | BRAKER3, MAKER, HISAT2/StringTie/IsoQuant, GeMoMa |
| function_annotation | DIAMOND blastp with database provenance gate | InterProScan, eggNOG-mapper, KofamScan |

## Architecture

```text
app/
  graph/        parent LangGraph, route controller, checkpoint factory
  stages/       one subgraph per biological workflow stage
  schemas/      typed contracts for state, assets, plans, descriptors, artifacts
  execution/    env resolution, frozen attempts, local runner, artifact collection
  review/       review gate registry, policies, recommendation/resume helpers
  control/      durable ledger and lifecycle inspection
  reports/      operator reports and run traces
  entrypoints/  CLI, ask flow, TUI, review/resume commands

docs/
  architecture.md          system architecture and ownership boundaries
  stage_contracts.md      stage-by-stage input/output contracts
  beginner_mainline_guide.md
                           beginner-friendly source reading map
  handoff.md              current status, validation evidence, known gaps
```

The parent graph intentionally stays thin:

```text
START -> route_controller -> <stage_subgraph> -> route_controller -> END
```

Stage subgraphs own method planning, validation, execution handoff, artifact
collection and review boundaries. This keeps biological method choice out of
the parent router and makes each stage independently testable.

## How To Run It

Gene Agent 有三层运行控制。实际使用时通常先用 TUI/ask 选流程，再用
run-config 固化执行参数，最后由各 stage 按输入和目标自动决定执行、跳过或
进入人工 gate。

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
2. 解析这句指令，生成 `AskPlanState`，并 materialize manifest。
3. 进入 `Plan Draft` 页面，展示 session、data dir、manifest、inputs、stages、methods、parameters 和 attention 项。
4. 在 draft 里可以按 `Enter/c` 继续，按 stage 编号编辑单个 stage，按 `s` 编辑 stage list，按 `r` 重新手动规划。
5. 进入单个 stage 后，可以按 `m` 改 method，按 `p` 改 parameters，按 `b` 返回 draft。
6. draft 锁定后不会立刻执行；TUI 会再展示 final run plan 并询问 `Proceed?`。
7. 用户确认后，TUI 写出 ask run config，把选中的 stage 写成 `requested_stages`，把可执行 stage 写成 `local_exec`。
8. TUI 在同一进程内构造 structured run 参数并调用 run session 逻辑；不是再启动一个新的 shell 命令。
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

TUI/ask 最终会把选择落到 run config：

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
    "read_type": "ont_raw",
    "estimated_genome_size": 500000000
  },
  "read_qc": {
    "length_filter": {
      "method": "read_qc.filter.seqkit_seq",
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
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 500000000,
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

`local_exec` 是安全阀：只有列在这里的 stage 才会尝试执行本地工具。没有列入
`local_exec` 的 stage 仍然可以参与规划、写 artifact、进入 review gate 或作为
下游 contract 继续传递，但不会偷偷运行昂贵命令。

结构化运行命令：

```bash
gene-agent run \
  --session-id <SESSION> \
  --manifest /absolute/path/to/manifest.yaml \
  --data-dir /absolute/path/to/data \
  --run-config /absolute/path/to/run_config.json \
  --checkpointer sqlite
```

也可以用 `ask` 生成 plan/run config：

```bash
gene-agent ask "assemble this ONT genome and annotate it" \
  --data-dir /absolute/path/to/data
```

非交互场景可以先让 `ask` 输出澄清模板，补完后再继续同一个 ask plan。

### 3. Stage Layer: automatic skip and gates

即使 `requested_stages` 里包含某个 stage，stage 内部仍会根据 manifest、
artifact registry、目标级别和已有产物决定执行、跳过或阻塞。

几个实际例子：

- **existing assembly input**：如果 manifest 只提供已有 assembly，inventory 会
  直接把主线推进到 `assembly_review`，并记录 `read_qc`、`genome_survey`、
  `assembly` 的 skip reason。
- **Hi-C scaffolding**：`hic_scaffolding` 会读取 inventory 里的
  `target_assembly_level`。如果没有 Hi-C reads/alignment/merged_nodups，会写
  `skipped_no_hic`；如果目标不是 `chromosome`，会写
  `skipped_not_chromosome_level`，下游 annotation 继续使用 pre-Hi-C assembly。
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

如果 `target_assembly_level: contig`，即使 `requested_stages` 里有
`hic_scaffolding`，Hi-C stage 也会走明确 skip 路径，而不是默认做染色体挂载。

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
- Unsupported methods are represented as planning contracts instead of fake success.
- Function annotation requires explicit database provenance before terminal completion.
- Large biological files stay outside graph state.

Known remaining work is tracked in `docs/handoff.md` and the phase documents
under `docs/phases/`.

## Read More

- `docs/architecture.md`：系统架构和模块边界
- `docs/stage_contracts.md`：各阶段输入/输出合同
- `docs/beginner_mainline_guide.md`：从业务到源码的阅读地图
- `docs/handoff.md`：当前状态、验证记录和已知风险
- `docs/deployment/mac_dev_server_pull.md`：Mac 开发与服务器/HPC 执行边界
