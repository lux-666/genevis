# Gene Agent

Gene Agent 是面向长读长基因组组装的可恢复、可审计工作流系统。它用
LangGraph 编排已批准的本地生信工具，把 manifest、参数推荐、人工决策、
运行证据和阶段产物保存在同一个 session 边界内。

当前可直接使用的 raw-read contig 路径完整保留五个分析阶段：

```text
Read QC (`read_qc`)
-> Genome Survey (`genome_survey`)
-> Assembly (`assembly`)
-> Assembly Dedup (`assembly_dedup`)
-> Assembly Review (`assembly_review`)
```

Inventory 会在冻结计划时作为隐式第一步加入。完整的 stage 合同、可执行方法和
当前实现状态分别由
[stage contracts](docs/architecture/reference/stage-contracts.md)、
[descriptor registry](descriptors/registry.yaml) 和
[implementation map](docs/architecture/langgraph-implementation-map.md) 维护；
README 不复制第二份方法表或完整工作流数组。

## Current Capabilities

- 结构化 `manifest + RunConfig` 的确定性启动入口；
- 可选 TUI intake 与可编辑规划；
- occurrence 级本地执行授权，允许同一 stage 重复出现；
- 方法级 freeze/run/collect 与可验证 Artifact/Ledger 证据；
- SQLite checkpoint、人工 gate、`inspect`、`review` 和 `resume`；
- 完整五阶段 contig 路径，以及非默认后续路径中已接线的 Hi-C scaffolding
  与 T2T 能力；
- 对未实现的 GapCloser/Base Polish fail closed，不伪装成功。

精确运行、冻结、进度和恢复语义由
[run-data contract](docs/architecture/reference/data-contract.md) 等 reference owner
维护；[runtime orchestration](docs/architecture/topics/runtime-orchestration.md) 只负责解释
这些合同如何连接。

## Architecture at a Glance

Gene Agent 把操作入口、冻结的运行事实、生物阶段图、方法执行和持久证据
分开管理。父图刻意保持很薄：

```text
START -> route_controller -> <stage_subgraph> -> route_controller -> END
```

`route_controller` 只按冻结的 occurrence 计划路由；每个 stage subgraph 自己负责
阶段内方法规划、验证、执行交接、产物收集和人工 gate。大型文件和日志
始终留在运行目录/Artifact/Ledger 边界，不塞进 top-level graph state。

```text
app/
  entrypoints/  public CLI, TUI, inspect, review and resume boundaries
  workflow.py   stage registry and canonical recommendation helpers
  graph/        thin parent graph, routing and checkpoint integration
  control/      frozen occurrence lifecycle, settlement, progress and resume
  stages/       one biological subgraph per registered stage
  execution/    environment resolution, attempt freeze, runner and evidence
  storage/      run layout, Artifact/Resource indexes and Ledger
  schemas/      typed input, plan, state and evidence contracts
  review/       human-gate registry and decision application
  reports/      operator-facing projections without control authority
```

开发者从 [Developer Guide](docs/developer_guide/README.md) 进入权威 architecture map；
操作员从 [User Guide](docs/user_guide/README.md) 进入安装、运行和恢复文档。
这个概览保留系统边界，但不成为 schema、workflow 数组或默认值的第二权威。

## Host Boundary

Mac 开发机负责编辑和代码测试；真实生信工具在 Linux 服务器或 HPC checkout 中由
同一个 CLI 本地执行。Gene Agent 不提供内置 SSH、远程 runner 或调度器提交后端。

真实 contig 运行需要：

- Python 3.11+ 的 `gene_agent` orchestrator 环境；
- 所选方法对应的 Conda 工具环境；
- Assembly 参数推荐所需的可达 LLM endpoint；
- Assembly Review 所需的 NCBI 快照和至少一个匹配的已安装 BUSCO lineage；
- 足够的 CPU、内存和存储。GPU 只在所选方法本身需要时才是要求。

新主机安装见 [New Host Setup](docs/deployment/migration.md)。Mac/server 的 Git
交付边界见 [Mac Development and Server Execution](docs/deployment/mac_dev_server_pull.md)。

## Quickstart: Five-stage Contig Run

### 1. Install the orchestrator

```bash
conda create -n gene_agent python=3.11 -y
conda run -n gene_agent pip install -e '.[dev]'
```

Python 依赖的唯一权威是 [pyproject.toml](pyproject.toml)。

### 2. Prepare and check the host

```bash
conda run -n gene_agent gene-agent bootstrap
# 审阅计划后，需要写入配置时再执行：
conda run -n gene_agent gene-agent bootstrap --apply

bash scripts/server_check.sh
bash scripts/server_test.sh
```

`docs/examples/envs.yaml.example` 是提交到 Git 的通用种子；bootstrap 生成或更新的
`config/envs.yaml` 是被忽略的本机配置，不再作为仓库共享配置提交。
`server_check.sh` 检查 orchestrator 前置条件和权威 contig 模板中固定 stage 的
工具/package closure，以及主机配置中 BUSCO/NCBI 数据库的结构与完整性；Assembly
方法会在上游证据产生后于 stage 内推荐，并从冻结的环境快照检查所选候选。真实
`gene-agent run` 还会检查输入和 LLM。
`server_test.sh` 只做代码回归、
compile 和 diff 检查，不是真实数据生信 smoke test。

### 3. Copy the authoritative templates

```bash
cp docs/examples/contig_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/contig_run_config.template.json /server/path/run_config.json
```

替换 manifest 中的 session/sample、基因组大小、reads 路径和平台；权威 RunConfig
模板不重复这些 manifest 事实。`user_metadata.target_species` 必须填写完整的
NCBI 拉丁学名，不能填写俗名或缩写。主机工具以及 BUSCO/NCBI 数据库实体只在
`config/envs.yaml` 配置一次；系统从物种名自动选择 BUSCO lineage，RunConfig
只填写执行参数（如 `threads`），不填写 lineage、数据库路径、版本或来源。
contig 模板也不预填 assembler 或 Assembly 参数：Assembly 会在 QC/survey 结果可用后
推荐方法，再推荐参数。

不要把真实输入路径、样本信息、BUSCO 数据、`.env` 或运行目录提交到 Git。

### 4. Start the run

```bash
gene-agent run \
  --manifest /server/path/manifest.yaml \
  --run-config /server/path/run_config.json \
  --data-dir /server/path/data
```

新运行需要 stdin/stdout 都连接真实 TTY。计划顺序、重复 occurrence、本地执行授权和
gate 行为来自已验证并冻结的 RunConfig，README 不维护它们的第二份精确说明。

### 5. Inspect or recover

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data
```

人工 review、decision template、恢复命令、`inspect --trace` 与 session summary 的区别
见唯一操作手册 [Run and Recovery](docs/user_guide/run-and-recovery.md)。

## Current Boundaries

- 本地进程执行必须由对应 occurrence 显式授权；
- 大型 FASTQ/FASTA/BAM、日志和结果表不进入 LangGraph state；
- `gapcloser`、`base_polish` 当前是 manual-only placeholder；
- 当前版本没有 checkpoint migration。更新含活动 session 的服务器 checkout 前，先
  完成运行或保留旧 checkout 与对应环境；
- 本机缺少真实生信工具只说明本机不是已配置执行主机，不等同于产品回归失败。

## Documentation Authority

- [Documentation Map](docs/README.md)：仓库级权威矩阵和文档维护规则；
- [User Guide](docs/user_guide/README.md)：操作员的安装、运行、gate 和恢复入口；
- [Developer Guide](docs/developer_guide/README.md)：开发者按改动类型进入权威合同和代码；
- [Architecture Map](docs/architecture/README.md)：按问题选择 topic/reference；
- [Run and Recovery](docs/user_guide/run-and-recovery.md)：唯一操作员运行与恢复手册；
- [Parameter Recommendation](docs/user_guide/parameter_recommendation.md)：操作者如何查看和批准参数推荐；
- [New Host Setup](docs/deployment/migration.md)：新主机安装；
- [Mac/Server Workflow](docs/deployment/mac_dev_server_pull.md)：Git 同步和服务器更新；
- [Configuration Examples](docs/examples/)：可复制、schema-valid 的输入模板。
