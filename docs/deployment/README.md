<a id="deployment-entry-points"></a>

# 部署入口

本页是部署边界图，区分主机准备与运行证据验收。运行、冻结、评审和恢复契约由[运行与恢复](../user_guide/run-and-recovery.md)拥有，本页不重复定义。

<a id="one-authority-per-operation"></a>

## 每项操作一个权威入口

| 操作 | 权威入口 | 职责 | 不负责 |
|---|---|---|---|
| Bootstrap | 可选 `gene-agent bootstrap` / `bootstrap --update` 助手 | 生成、扩展、验证活动环境映射、声明的包探针及映射内每个方法项 | 映射独占写入、包安装、生物学输入或运行会话 |
| 主机就绪 | [`scripts/server_check.sh`](../../scripts/server_check.sh) | 只读检查编排器前置条件、显式 RunConfig 的固定阶段方法/包闭包和主机数据库；默认五阶段 contig 模板 | 延迟 Assembly 候选就绪、LLM 可达性、私有输入或生物学执行 |
| 代码验证 | [`scripts/server_test.sh`](../../scripts/server_test.sh) | 格式、lint、类型检查、普通 pytest、compileall、diff 检查 | 主机就绪或真实数据端到端验收证明 |
| 路线/切片证据 | `python -m app.diagnostics.route_acceptance --route SCOPE --run-dir PATH` | 只读范围内进程/收集证据、交接、请求时的最终 Review、抛光范围的成对评估，以及可选 Hi-C 不重跑比较 | 运行工具、改变决策、独立 QV 或保证生物学改善 |

<a id="host-configuration-lifecycle"></a>

## 主机配置生命周期

`docs/examples/envs.yaml.example` 是通用种子，只在活动主机映射缺失时供初始化。默认活动映射 `config/envs.yaml` 是机器本地部署状态，必须保持忽略/未跟踪。所有公共就绪路径选择同一映射：显式 `bootstrap --config PATH` 对该次 bootstrap 优先，其次 `GENE_AGENT_ENVS_CONFIG`，最后仓库默认。命令可能从检出目录外启动时，应使用绝对覆盖路径；相对覆盖按调用者当前目录解析，默认路径锚定检出目录。

操作者可直接创建和编辑活动映射及项目 `.env`，bootstrap 可选。人工和辅助编辑使用同一运行格式，之后均可运行 bootstrap 校验。

默认 bootstrap 采集核心 Conda 目标，并询问是否配置可选 Assembly Review 数据库和 AutoHiC 组。资源尚未安装时可以跳过，bootstrap 仍预览并发布其余有效计划。以后重跑普通命令或手改 YAML 添加缺失组。`--update` 通过同一预览/确认流程替换已管理的选定字段。

Bootstrap 检查活动映射中每个声明的包、方法、数据库，包括主机暴露的 Assembly 候选。省略可选组不是 bootstrap 错误；但 `server_check.sh` 或确认运行的就绪检查在工作流需要该组时仍会拒绝。

Bootstrap 不安装 Conda 环境、工具、数据库、BUSCO lineage、模型或 LLM 服务。它们由操作者配置，通过活动映射或私有环境配置引用。

命令假定当前 shell 已激活文档约定的 `gene_agent` 编排器环境。该名称是安装惯例，不从主机映射解析；工具环境才从映射解析。

```bash
gene-agent bootstrap
```

该命令是辅助配置入口，不是执行的强制前置步骤。人工准备的活动映射可直接进入相应就绪检查。

<a id="verification-sequence"></a>

## 验证顺序

Python 运行版本由 [`app/version.py`](../../app/version.py) 单一拥有，
`pyproject.toml` 的 Hatch 元数据从该文件读取；不要在 `app/__init__.py` 或
部署脚本另写版本号。应用要求 Python 3.11+，CI 固定 Python 3.12。运行时和开发
依赖的可复现解析边界是 [`constraints/ci.txt`](../../constraints/ci.txt)，它锁定
CI/部署回归所验证的直接闭包版本；`pyproject.toml` 仍是依赖声明的权威来源。

安装并激活编排器环境后立即运行代码验证。CI 使用同一约束文件安装 editable
wheel 并调用现有 `scripts/server_test.sh`，不维护第二套测试或 scanner。主机配置完成后检查 contig 就绪：

```bash
bash /path/to/gene_agent/scripts/server_test.sh
bash /path/to/gene_agent/scripts/server_check.sh
```

`server_check.sh` 使用 `gene-agent run` 共用 Python 就绪代码导出固定阶段方法闭包，刻意不维护 shell 工具列表。Assembly 方法选择延迟到上游证据具备后，候选在 Assembly 入口从冻结依赖快照解析并检查。规范 contig 模板含 Assembly Review，因此也校验活动映射中 BUSCO lineage 与 NCBI 根下快照。真实运行还校验 manifest 和所选 LLM 端点。独立切片或完整 HiFi 路线应将编辑后的 RunConfig 作为脚本首参。模板、执行、分离 Hi-C 恢复及范围化运行后检查由[运行与恢复](../user_guide/run-and-recovery.md#independent-slice-server-runbook)拥有。

Bootstrap 成功不证明 `server_check.sh` 一定成功；`server_check.sh` 成功也不证明活动映射的全部声明健康。两者范围不同，应在实际执行的检出目录检查。若 bootstrap 使用 `--config PATH`，则为 `server_check.sh` 和真实运行导出同一绝对 `GENE_AGENT_ENVS_CONFIG`。

`server_test.sh` 是代码级回归门禁，运行格式、lint、mypy、普通 pytest、compileall 和 `git diff --check`，明确排除 `real_tool`。它不是实际数据或生物工具冒烟框架。完整路线前按[分层验收顺序](../user_guide/run-and-recovery.md#acceptance-order-code-tests-before-real-end-to-end-runs)执行：代码测试、小工具冒烟、contig 端到端、独立 Hi-C/finishing 切片、完整真实端到端。复用操作者已有五阶段运行证据，不强迫重跑已覆盖方法。未关闭的全路径验收边界只在[架构一致性](../architecture/reference/conformance.md#five-stage-real-tool-contig-acceptance-is-not-automated)记录。

新主机安装见[新主机配置](migration.md)；Mac/Git 交接见[Mac 开发与服务器执行](mac_dev_server_pull.md)；启动和恢复会话见[运行与恢复](../user_guide/run-and-recovery.md)。
