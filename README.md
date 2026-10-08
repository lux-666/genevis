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
- GapCloser 支持 HiFi + TGS-GapCloser，执行后人工采纳；Base Polish 支持 HiFi + Racon 单轮修正、前后比较和人工保留原稿。

完整 HiFi + Hi-C 路线的配置、实跑及产物核验入口见
[服务器验收说明](docs/user_guide/run-and-recovery.md#complete-hifihic-server-acceptance)。
最终 Review 同口径评价原稿与修正稿，不以 FASTA 存在或 N50 上升判定成功。

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
conda activate gene_agent
python -m pip install -e '.[dev]'
bash scripts/server_test.sh
```

Python 依赖的唯一权威是 [pyproject.toml](pyproject.toml)。`gene_agent` 是本文档
约定的编排器环境名，不从 `config/envs.yaml` 读取；激活后所有操作命令都直接使用
当前环境。生信工具的 Conda 环境才由 `config/envs.yaml` 解析。

### 2. Prepare and check the host

```bash
gene-agent bootstrap

bash scripts/server_check.sh
```

`config/envs.yaml` 和 `.env` 可以直接手动维护；`bootstrap` 是可选的生成、补填和
校验助手。数据库或 AutoHiC 尚未安装时，可以先运行 bootstrap 并跳过对应配置组，
以后安装完成后再手动补入，或重新运行普通 `gene-agent bootstrap` 选择补填。每次
bootstrap 都在同一次调用中展示候选计划并等待确认，确认后才写入；不会重新收集
第二份计划。

`docs/examples/envs.yaml.example` 是提交到 Git 的通用种子；bootstrap 生成或更新的
`config/envs.yaml` 是被忽略的本机配置，不再作为仓库共享配置提交。
`server_check.sh` 检查 orchestrator 前置条件和权威 contig 模板中固定 stage 的
工具/package closure，以及主机配置中 BUSCO/NCBI 数据库的结构与完整性；Assembly
方法会在上游证据产生后于 stage 内推荐，并从冻结的环境快照检查所选候选。真实
`gene-agent run` 还会检查输入和 LLM。因此部分 bootstrap 成功不代表完整 contig
环境已经就绪。
`server_test.sh` 执行 format、lint、mypy、代码回归、compile 和 diff 检查，
明确排除 `real_tool`；真实工具检查通过 `pytest -m real_tool` 单独选择。

### 3. Copy the authoritative templates

```bash
cp docs/examples/contig_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/contig_run_config.template.json /server/path/run_config.json
```

替换 manifest 中的 session/sample、基因组大小、reads 路径和平台；权威 RunConfig
模板不重复这些 manifest 事实。`user_metadata.target_species` 必须填写完整的
NCBI 拉丁学名，不能填写俗名或缩写。主机工具以及 BUSCO/NCBI 数据库实体只在
`config/envs.yaml` 配置一次；系统从物种名自动选择 BUSCO lineage，RunConfig
只填写执行参数（如 `threads`）和非 manifest 生物学事实（如 `ploidy`），不填写
lineage、数据库路径、版本或来源。运行前按样品核对模板中的 ploidy 和各阶段线程数。
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
- `gapcloser` 目前仅支持 HiFi 无纠错路线，产物须人工采纳；参见
  [单产物运行示例](docs/user_guide/run-and-recovery.md#hifi-gap-closing-from-an-existing-assembly)；
- `base_polish` 运行 HiFi + Racon，人工决定采纳或保留原稿，最终 Review 比较两者；
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

## Assembly v5 case consumption

Assembly 每次新方法推荐和参数推荐（含重试）直接读取配置的活动案例目录。
配置 `assembly.case_source` 指向包含 `recommendation-cases.v1` JSON 的目录
（相对路径以配置文件为基准）；不配置时只用当前材料与软件知识，不回退旧案例库。
来源方法、原生参数、材料状态和结果限定均保留，跨方法案例可参与推理，
但执行输出只能包含已确认方法的 L2 参数。
每条 case 的 `parameter_eligible` 单独标记是否有可用于调参的明确设置；
参数检索只排序 `true`，方法路线检索仍使用全部 case。
**case JSON 是唯一维护和交付依据**：资格、指标、补充证据和来源定位直接保存在 case 内，
不再维护独立资格表、全库 selection 或补充回放文件。

本地 case 统一放在 `descriptors/knowledge_packs/assembly/cases/`，由 Git 忽略。
使用真实加载器验证，再将这些 JSON 手动复制到服务器的活动案例目录：

```bash
python -c 'from app.knowledge.assembly_cases import load_case_papers; papers = load_case_papers("descriptors/knowledge_packs/assembly/cases"); print(len(papers), "papers", sum(len(p.cases) for p in papers), "cases")'
```

复制后配置 `assembly.case_source`；程序不会自动扫描本地目录。
修订既有 case 时直接更新该文件，保留原生参数、材料绑定和来源证据，然后重新验证。
修改或复制活动目录前，等待进行中的推荐结束并暂停新推荐和重试；全部文件复制并验证后再恢复。
Git 不携带这些案例数据，部署和备份应保留已校验的 case 文件；原始图重新投影不能恢复后续补充。

新论文仍可通过 `scripts/build_assembly_cases.py` 从 `assembly-extraction.v5` 图一次性导入。
调用方为本批新 case 提供临时选择文件，每项显式填写 `parameter_eligible`，不读取全局补充表。
输出到暂存目录，审核后加入活动案例库；不要用原图批量重建并替换已修订的 case。

```bash
python scripts/build_assembly_cases.py \
  --extractions /path/to/new-source-graphs \
  --selection /path/to/new-case-bindings.json \
  --filename-template '{paper}-response.json' \
  --output /path/to/staged-new-cases
```

该工具不修改原图，不覆盖不同内容，不将投影视为论文重新审核。
选择文件中的每项必须明确 `paper`、`step`、`stage: assembly`、
`task: contig_assembly` 和输入 state ID 到平台的 `inputs` 映射；
`features` 指向 measurement ID 并声明规范单位，`outcomes` 列出结果 measurement ID。
显式倍性标量使用 `unit: count`；不从 `diploid`、`2n=34` 或 coverage 推断数值倍性。
`input_states` 只在原图证据明确时填写，否则保留 `unknown`，不当作 raw/filtered 通配符。
不创建运行级案例快照，也不把完整 v5 实体复制进 case。
每次推荐只记录实际选中的紧凑证据；恢复同一次已记录请求不改写历史内容。
case 保留方法原生参数、材料/结果对象、评价口径、来源定位和缺口。
一个 case 只对应一次 Assembly 操作；Hi-C 参与组装器分相的直接输入保留，
后续 scaffolding/T2T 不混入该操作参数。结果明确区分直接输出、下游上下文和混合对象。
旧 casepack/V1/pilot 配置不兼容。Hi-C、T2T 推荐器的扩展不在本次范围。
指标绑定及推荐效果的待验收边界见
[Assembly corpus conformance](docs/architecture/reference/conformance.md#assembly-corpus-handoff-and-measured-recommendation-coverage)。
案例可加载、可检索不等于参数资格、指标绑定或推荐效果已完成验收。
精确合同见 [Assembly case consumption](docs/architecture/reference/casepack.md)。
