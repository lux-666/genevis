<a id="project-bootstrap"></a>

# 项目 Bootstrap

> **状态：** 已实现，为可选机器配置助手
>
> **阅读场景：** 初始化、手动编辑或修复 `config/envs.yaml` 与 `.env`，或在冻结运行前诊断已配置包与 Conda 就绪状态。
>
> **来源：** 操作者机器路径和环境选择、主机方法目录、软件包/数据库声明。
>
> **下游：** [TUI 与规划](tui-and-planning.md)、[方法执行](execution.md)使用的依赖快照，以及 [AutoHiC](autohic.md)运行需求。
>
> **依赖契约：** [描述符](../reference/descriptors.md)、[治理](../reference/governance.md)、[运行数据](../reference/data-contract.md)。
>
> **代码依据：** `app/entrypoints/run.py::_run_bootstrap`、`app/execution/project_bootstrap.py`、`app/execution/package_bootstrap.py`、`app/execution/method_preflight.py`、`app/execution/env_resolver.py`、`tests/test_project_bootstrap.py`。

<a id="boundary"></a>

## 边界

Bootstrap 准备可变机器/项目配置，不是工作流运行，不拥有生物学输入，也不是每次执行的必经步骤。操作者可直接创建或编辑 `config/envs.yaml`、`.env`；bootstrap 是生成、扩展、验证这些文件并实施有限布局修复的辅助入口。

`docs/examples/envs.yaml.example` 是已提交的通用种子。默认活动 `config/envs.yaml` 是被 Git 忽略的机器本地状态，由 bootstrap 或操作者创建；活动文件缺失时，执行绝不回退到示例。活动配置选择与执行共享优先级：显式 `bootstrap --config PATH`、`GENE_AGENT_ENVS_CONFIG`、仓库默认值。调用者可能使用不同当前目录时，覆盖路径应为绝对路径：相对覆盖跟随调用者当前目录，默认 `config/envs.yaml` 则锚定检出目录。

| Bootstrap 拥有 | Bootstrap 不拥有 |
|---|---|
| 辅助创建和修复活动环境/包/数据库映射 | 映射独占写入权；支持操作者直接编辑 |
| 项目 `.env` 中选定 `GENE_AGENT_*` 覆盖，隐藏并保护秘密 | 强制 `.env` 必须由 bootstrap 创建 |
| 验证每个已声明环境、包、数据库项 | 将未声明的可选数据库或 AutoHiC 组判为非法 |
| 按执行时优先级探测配置的方法环境 | 证明任意未来工作流就绪 |
| 已声明的 Juicer 布局修复 | 安装任意工具、Conda 环境、参考数据库或工作流软件包 |
| 预览并原子发布选定文件改动 | 冻结会话或重建既有会话依赖快照 |

读段、组装、物种元数据、限制性内切酶、阶段选择和评审决策等每运行事实属于 TUI/运行输入与持久运行证据，放进 `.env` 或 `config/envs.yaml` 会越界。

<a id="manual-and-assisted-configuration"></a>

## 人工与辅助配置

手动编辑和 bootstrap 产出同一活动配置契约，没有 bootstrap 标记、所有权位或备用运行格式。手动编辑后可运行 bootstrap 校验；bootstrap 编辑后仍可继续人工维护。

主机能力可逐步配置：

1. 用共享 Conda 环境或 prefix 创建核心映射。
2. 尚未安装时，省略 Assembly Review 数据库或 AutoHiC。
3. 之后安装所需资源。
4. 手动添加完整 YAML 组，或重跑普通 `gene-agent bootstrap` 并选择配置缺失组。

省略可选组表示“该主机尚未配置”。已存在的组仍严格校验：占位符、缺必需字段、路径不存在、数据库内容无效或包探针缺失均为错误。选择未配置能力的工作流在冻结前的运行就绪检查失败。

<a id="modes-and-write-behavior"></a>

## 模式与写入行为

默认命令构建并验证一个计划，打印将执行的全部配置变更和声明的包布局修复，等待空回车确认后应用同一内存计划，不退出再在下一次调用重建第二份计划。

活动文件缺失时，bootstrap 总是采集共享 Conda 目标，分别询问是否配置 Assembly Review 数据库和 AutoHiC。跳过可选组会从候选中移除占位符，不阻止写入 `config/envs.yaml` 或独立 `.env` 改动。以后重跑普通命令仍会询问缺失的可选组。

`--update` 是既有 bootstrap 管理字段的交互替换入口：操作者选择字段，查看 `old -> new`，遵守同一最终确认规则。添加当前缺失数据库或 AutoHiC 组使用普通命令；`--update` 不维护第二 schema，也不配置缺失组。没有独立 `--apply` 模式。

有效现有值除非显式选择更新，否则保留。候选配置通过执行共用的 `EnvResolver` 加载；过期计划和未记录变更检查防止静默覆盖并发编辑。秘密不打印，新建或更新的 `.env` 使用仅当前用户权限。

<a id="validation-and-conformance"></a>

## 验证与布局修复

有可持久读取的候选后，bootstrap：

1. 验证环境和软件包引用。
2. 包选择启用且模式授权写入时，执行已实现的 Juicer 符号链接修复。
3. 验证所有已声明包的探针文件。
4. 按 Conda 目标分组配置的方法，并探测必需可执行文件。
5. 验证所有声明的 BUSCO lineage，以及配置的 NCBI 根目录直属快照。
6. 返回前报告所有配置包、方法、数据库、目标、缺少的可执行文件和修复提示。

Bootstrap 验证已声明范围，不证明所有可能工作流的安装完整性。`server_check.sh` 拥有文档规定的 contig 配置整体就绪（含 BUSCO/NCBI），已确认运行的检查只覆盖精确选定闭包。因此核心配置 bootstrap 可成功，后来 contig 就绪检查仍可因缺数据库或包而失败。

这是就绪验证，不是安装。唯一已实现包布局修改是包配置声明的有限 Juicer 链接修复。尚未实现通用环境求解器、下载器、调度器设置或包安装器，不能从“bootstrap”一词推断这些能力。

<a id="relationship-to-the-two-freezes"></a>

## 与两个冻结边界的关系

`config/envs.yaml` 是可变机器状态。操作者之后确认 TUI 计划时，整次运行冻结将选定环境/包闭包及必需类型化数据库注册表捕获到 `control/dependency_manifest.json`。冻结执行器从快照解析，不使用后续 bootstrap 或手动修改。每个可执行方法再从已批准输入与冻结依赖创建更窄的方法尝试冻结。

唯一隐式 dotenv 来源是活动主机映射所选项目旁的 `.env`：默认 `config/envs.yaml` 对应仓库根 `.env`；显式或 `GENE_AGENT_ENVS_CONFIG` 指向其他 `config/` 下路径时，对应那个项目根 `.env`。调用者当前目录的 `.env` 绝不是备用来源。已导出的变量仍通过 setdefault 加载保持优先。

Bootstrap 探测活动机器映射声明的每个包和方法。另行地，运行最终确认并通过 TTY 检查后，结构化 run 和 TUI 仅对规范有效方法闭包及该工作流必需 LLM/数据库服务做只读就绪探测。同一闭包随后冻结到依赖快照；运行专属探测失败不创建会话。

选定闭包内每个本地可执行方法，都必须通过活动配置解析到非空 `required_tools` 和/或 `package_tools` 声明。方法级项可覆盖阶段级声明，阶段级需求仍是合法用户配置。只有默认 Conda 目标或解析为空的需求集合必须拒绝，避免配置遗漏变为零工具通过。

<a id="database-key-normalization"></a>

## 数据库键规范化

主机数据库配置只接受 `databases.busco` 和 `databases.ncbi`。类型化加载器拒绝未知组；bootstrap 在询问或写入前按同一字段集合检查组名，修复已声明组内的缺失设置。运行时与冻结记录使用相同名称。
