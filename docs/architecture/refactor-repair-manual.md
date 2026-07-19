# 重构修理手册：收敛执行、选择、资源与运行时权威

> ⚠️ **重要提示：** 本手册已经过可执行性验证（详见 `refactor-status-analysis.md`）。预估工作量 **38-57 人天（6-8周）**，风险等级 **中-高**。请在实施前仔细阅读分析报告和补充文档。

## 执行摘要

本手册定义了基因组分析工作流系统的架构重构规范，核心目标是**建立唯一权威（Single Source of Truth）**，消除重复逻辑、兼容层和测试旁路。

**关键原则：**
- 每类事实只有一个所有者
- TUI 负责选择和默认值，运行层只执行明确指令
- 使用中央资源表进行跨 stage 交接
- 删除所有兼容代码，旧配置必须明确失败
- 测试通过依赖注入，不在生产代码中保留旁路

**重构范围：** 主线工作流（inventory → read_qc → genome_survey → assembly → assembly_dedup → assembly_review → hic_scaffolding → t2t）及共享基础设施。annotation 相关 stage 的业务重构延后。

**当前状态（2026-07-19）：**
- ✅ **已验证：** 需要修改的组件确实存在（`local_exec`、`AssemblySelection`、`assembly_route` 等）
- ⚠️ **完成度：10-15%** - 部分基础设施已存在（`ResourceSpec`、`STAGE_ORDER`），核心变更未开始
- 🔴 **高风险项：** Resource Handoff（8个stage）、删除 `AssemblySelection`
- 🟡 **中风险项：** 统一 execution_mode（影响测试但无需大量重写）
- 📋 **遗漏需求：** Parameter 系统架构、FOFN 策略、向后兼容性（详见补充文档）

**相关文档：**
- `refactor-status-analysis.md` - 详细的可执行性分析和风险评估
- `refactor-supplemental-requirements.md` - 补充需求说明
- `refactor-decision-points.md` - 关键决策讨论文档

---

## 1. 目的与边界

### 1.1 重构目标

这次重构的目标不是把现有分支逐个补齐，而是**删除重复权威、兼容扫描和测试旁路**，让每类事实只有一个所有者。

### 1.2 范围定义

**包含：**
- 共享基础设施（资源解析、路径管理、配置加载）
- 主线工作流：`inventory -> read_qc -> genome_survey -> assembly -> assembly_dedup -> assembly_review -> hic_scaffolding -> t2t`
- 执行授权、方法选择、参数传递机制

**不包含：**
- annotation 相关 stage 的业务行为重构
- annotation 的路由、review 和循环依赖（另开一轮）
- 共享 schema 变更引起的机械性调用调整可以做

### 1.3 执行原则

1. **不保留兼容层**：不保留旧字段、旧别名、旧解析顺序或兼容 shim
2. **明确失败**：旧配置应明确校验失败，而不是被静默转换
3. **渐进迁移**：先建立唯一权威，再迁移调用点，最后删除旧实现
4. **测试驱动**：测试验证目标架构，不为旧测试在生产代码中保留旁路
5. **显式参数**：工具命令的执行参数必须显式；运行时不能发明默认值
6. **分离关注点**：TUI 可以推荐和填默认值，但写给运行层的必须是完整、明确、可审计的指令

---

## 2. 核心架构变更

### 2.1 执行授权机制（修订方案）

**问题：** 当前系统有多套执行控制机制并存：
- `local_exec` 列表（CLI 和 RunConfig）
- `execution_mode=”local”` 参数（测试和运行时）
- stage-specific 的执行判断逻辑
- 导致执行语义不清晰，测试与生产代码分离

**解决方案：** 统一到 `execution_mode` 作为唯一执行控制机制。

**新规则：**
- `execution_mode` 是唯一的执行控制参数，取值：
  - `”auto”` (默认) - 自动判断：CLI `run` 命令执行，`plan` 命令不执行
  - `”execute”` - 强制执行（测试、特殊场景）
  - `”plan_only”` - 只规划不执行（preflight、dry-run）
- `requested_stages` 决定工作流经过哪些 stage
- Method 被选中 + `execution_mode != “plan_only”` = 执行该 method
- 删除 `local_exec` 列表，避免两套控制机制
- 所有 stage 使用统一的执行判断逻辑

**统一的执行逻辑：**
```python
def should_execute_stage(
    stage_name: str,
    method_config: MethodConfig | None,
    execution_mode: ExecutionMode
) -> bool:
    “””Unified execution authorization logic.”””
    # No method configured = planning only
    if method_config is None:
        return False
    
    # planner_only method cannot be executed
    if method_config.is_planner_only:
        return False
    
    # Respect execution mode
    if execution_mode == “plan_only”:
        return False
    
    # Method is configured and execution is authorized
    return True
```

**需要删除的组件：**
- `RunConfig.local_exec` 字段（用 `execution_mode` 替代）
- `LocalExecStage` 枚举
- CLI `--local-exec` 参数（用 `--execution-mode` 替代）
- stage-specific 的 `local_execution_not_enabled` 分支（统一为 `execution_mode` 判断）
- `validate_local_assembly_execution` 函数（改为通用的 `should_execute_stage`）

**需要统一的组件：**
- ✅ 保留 `execution_mode` 参数（已存在于测试中）
- ✅ 扩展为三值：`auto`, `execute`, `plan_only`
- ✅ 在 CLI、RunConfig、Runtime 统一使用
- ✅ 测试继续使用 `execution_mode=”execute”` 或 `”plan_only”`，无需大量修改

**迁移路径：**
1. 定义 `ExecutionMode = Literal[“auto”, “execute”, “plan_only”]`
2. 在 RunConfig 添加 `execution_mode: ExecutionMode = “auto”`
3. CLI 添加 `--execution-mode` 选项
4. 实现统一的 `should_execute_stage()` 函数
5. 各 stage 调用该函数而非各自判断
6. 弃用但保留 `local_exec` 字段一个过渡期，给出迁移提示
7. 测试从 `execution_mode=”local”` 改为 `execution_mode=”execute”`（简单替换）
8. 过渡期后删除 `local_exec` 相关代码

**优势：**
- ✅ 执行语义清晰：一个参数控制所有 stage
- ✅ 测试影响小：只需改参数值，不需重写测试逻辑
- ✅ 生产和测试使用同一机制
- ✅ 支持 dry-run 和 plan 命令
- ✅ 工作量从 5-7天降低到 2-3天

### 2.2 Assembly 方法选择

**问题：** 运行层和 TUI 层都在做方法选择，导致选择逻辑重复，难以追踪最终使用的方法。

**解决方案：** 运行层不做 assembly 方法选择，TUI 是唯一选择入口。

**新规则：**
- TUI 是选择入口，descriptor registry 是 TUI 可选 method 元数据的来源
- TUI 必须把最终选择写成唯一、完整的 canonical method ID，例如 `assembly.candidate.hifiasm_ont`
- run config 的 assembly block 只携带一个明确 method 及该 method 的完整参数
- manifest/read type 可以帮助 TUI 推荐和过滤不兼容选项，但运行时只能校验，不能据此选择或改选 method
- runtime 不维护 primary/fallback/recommendation/timeout 等第二套选择状态

**需要删除的组件：**
- `AssemblySelection` 类
- `assembly_selection` 字段
- `assembly_route` 字段
- `DEFAULT_PRIMARY_BY_READ_TYPE` 常量
- runtime 的 `default_primary_method` 逻辑
- short-name method alias 和 normalize 后继续运行的兼容行为
- ask 层和 run 层各自推导默认 assembler 的逻辑

**可以保留：** 纯校验能力，例如 canonical read type、method/read-type compatibility；它们不应带默认 method，也不应修改输入。

### 2.3 执行参数默认值

**问题：** 默认值分散在多个层次（descriptor、parameter catalog、runtime schema），导致默认值来源不明确。

**解决方案：** 所有会改变工具命令的默认值只属于 TUI/planner 层。

**新规则：**
- **唯一默认值来源**：method descriptor 的 `planner.parameter_defaults`
- TUI 读取这些默认值、展示给用户，并把确认后的具体值写入 run config
- parameter catalog 只描述类型、范围、枚举、互斥和命令映射，不再保存第二套默认值
- Pydantic runtime schema 对执行参数使用必填字段，不设置命令级 default
- method adapter/freeze/command builder 收到缺失参数时必须失败，不得 fallback
- 来自上游测量的值不是”默认值”。例如 genome survey 的测量结果可以作为有来源的 runtime input，但来源必须明确并进入 parameter evidence

**区分：**
- **工具参数**（不能默认）：threads、read type、cutoff、genome size 等命令输入
- **控制参数**（可以默认）：报告格式、review policy 等不会改变工具 argv 的稳定控制参数

### 2.4 资源交接机制

**问题：** 跨 stage 资源交接通过读取其他 stage 的私有 output key 实现，导致耦合严重、难以追踪。

**解决方案：** 使用中央资源表 `resource_refs`，不再读取其他 stage 的私有 output key。

#### 2.4.1 中央资源表设计

在 state 中增加 `resource_refs` 映射，只存引用和必要元数据。

**逻辑资源名定义：**
- `long_reads` - 长读长测序数据
- `short_reads` - 短读长测序数据
- `assembly` - 组装结果
- `masked_assembly` - 掩码后的组装
- `predicted_proteins` - 预测的蛋白质
- `hic_reads` - Hi-C 测序数据
- `genome_size_estimate` - 基因组大小估计

#### 2.4.2 资源交接规则

1. **初始注册**：`inventory` 把 manifest asset 注册成初始资源
2. **更新规则**：stage commit 只更新自己声明产出的资源
3. **替换机制**：同一条线上的新产物替换同名当前资源。例如 assembly、dedup、review、Hi-C、T2T 依次更新 `resource_refs["assembly"]`
4. **下游读取**：下游 stage 只按逻辑资源名读取，不知道上游 stage 的私有字段名
5. **私有数据**：stage 私有 `stage_outputs` 可以保留执行游标、指标和 evidence ref，但不得作为跨 stage 资源发现协议
6. **失败处理**：资源缺失或损坏时失败；不得反向扫描所有 stage 猜测候选

#### 2.4.3 FOFN 处理规范

**问题：** 当前使用 FOFN（file of filenames）作为跨 stage 协议，导致优先级和后缀猜测逻辑复杂。

**解决方案：**
- `long_reads` 的 canonical 表示应是一个或多个具体 read file ref，而不是 FOFN
- FOFN 是 NextDenovo、purge_dups 等 method adapter 的输入序列化格式
- 需要 FOFN 的 adapter 在自己的 work dir 中从 canonical read refs 生成
- 这样可以删除跨 stage 的 FOFN key 优先级和 suffix 猜测

#### 2.4.4 ResourceSpec 收窄

现有 `ResourceSpec` / `ResourceResolution` 可以收窄后复用，不需要再创建一套 `ReadInput`、`ReadTypes` 或 prompt profile 模型：

**保留：**
- `ResourceSpec` 只保留逻辑资源名、允许的 manifest asset type、是否必需、是否多值

**删除：**
- `prefer_stages` 和 `output_keys` 字段
- `resolve_stage_resource` 只查显式 override、`state.resource_refs`、初始 asset registry 三个已声明位置
- 动态 `final_<type>_for_downstream` 逻辑
- reverse `STAGE_ORDER` 扫描和 legacy key 扫描
- `app/domains/read_inputs.py` 不再承担资源选择。若其中只剩通用 FOFN 解码，可移动到实际需要接受外部 FOFN 的 I/O 边界；如果 canonical handoff 已不接收 FOFN，则直接删除
- `read_qc.make_fofn` 不应作为业务 method 参与 stage handoff；需要 FOFN 的工具 adapter 自己物化

### 2.5 Workflow 与 Method Registry 权威边界

**问题：** workflow、method registry、stage builder 等在多处重复定义相同信息。

**解决方案：** 让两种不同事实各有一个权威来源。

#### 2.5.1 权威分配表

| 事实类型 | 唯一权威 | 不再拥有它的地方 |
|---------|----------|-----------------|
| stage 名称、顺序、终止节点 | `app/workflow.py` | state、run config、graph 中的手写重复列表 |
| stage builder 绑定 | `app/graph/builder.py` 的一个 mapping | 第二份 node list、第二份 edge list |
| method ID、所属 stage、descriptor 路径、planner 元数据 | `descriptors/registry.yaml` + descriptor | Python method 常量表、TUI 手写 method catalog |
| stage 需要/产出哪些逻辑资源 | `app/stages/resource_specs.py` | preflight 模拟表、各 stage 私有 key 搜索 |
| 工具环境与 required tools | env config | stage 的 PATH 探测和测试注入 |

#### 2.5.2 具体收敛方式

1. **Workflow 集成**
   - `DomainStage`/`RequestableStage` 从 `app/workflow.py` 导入或用其中的集合校验，不再各自列一遍 Literal

2. **Graph Builder**
   - graph builder 迭代 `STAGE_ORDER`，用一个 `STAGE_BUILDERS` mapping 建 node 和 edge
   - 删除手写两轮 stage node 名单

3. **Registry 校验**
   - registry loader 校验 mapping key 必须等于 `descriptor.method_name`
   - method 的 `stage` 必须存在于 workflow
   - 每个 method 在 `by_stage` 中恰好出现一次

4. **统一访问**
   - 所有 planner、stage 和 method adapter 通过一个 `load_descriptor_registry()` facade 取 registry
   - 不再自行写路径和加载逻辑

5. **测试简化**
   - 测试只验证 authority 与 implementation binding 集合完全相等
   - 不维护第三份 `WIRED_METHODS` 真值表

---

## 3. CWD 路径问题

### 3.1 问题描述

CWD 是启动进程时所在的目录。`Path(“descriptors/registry.yaml”)` 表示”当前目录下的 descriptors”，不是”本项目的 descriptors”。因此在仓库根目录运行时看似正常，从 `/tmp`、IDE、scheduler 或安装后的 console script 启动时就会找错位置。

**当前受影响的组件：**
- 多个 stage 和 method adapter 直接加载 `Path(“descriptors/registry.yaml”)`
- 默认 env config 使用 `Path(“config/envs.yaml”)`
- planner catalog 的路径解析

### 3.2 路径规则

路径规则应一次性定义：

1. **内置只读资源**：相对包资源定位，不相对 CWD
2. **CLI 传入路径**：在 entrypoint 解析一次并变成绝对路径
3. **Manifest 内相对路径**：相对 manifest 文件所在目录
4. **Run config 内相对路径**：相对 run config 文件所在目录，或者直接要求 TUI 物化为绝对路径；二选一后全局统一
5. **Env config 内 install root**：若允许相对路径，应相对 env config 文件所在目录
6. **Session 内引用**：通过 `SessionPaths` 解析，不能回退到 CWD

### 3.3 推荐实现

1. **配置 Facade**
   - 在一个配置 facade 中提供 `load_descriptor_registry()` 和 `active_env_config_path()`
   - immutable descriptors 作为 package data，通过 `importlib.resources` 获取
   - 不要在每个调用点传默认 `Path`

2. **Env Config**
   - host-specific env config 由显式 CLI/env override 指定
   - 开发 checkout 的默认配置若保留，也必须锚定到模块位置，而不是 CWD

3. **Package Data**
   - `pyproject.toml` 必须把运行所需 descriptor 资源包含进 wheel
   - 否则 console script 只安装 `app` 时仍然会缺资源

### 3.4 验收标准

必须从仓库外运行：

```bash
cd /tmp
gene-agent inspect --help
gene-agent run --manifest /absolute/path/to/manifest.yaml --run-config /absolute/path/to/run.json
```

descriptor catalog 的单元测试也应在临时 CWD 下加载成功。

---

## 4. Preflight 与 Runtime Contract

### 4.1 问题描述

Preflight 不应该模拟 stage 私有输出，也不应该维护 runtime contract 的影子。

### 4.2 中央 Stage Contract

中央 stage contract 同时声明：

- `requires`: 逻辑资源名及 required/optional
- `produces`: 成功 commit 后产生或替换的逻辑资源名
- `methods`: 从 descriptor registry 反查，不重复声明 method metadata

### 4.3 静态 Preflight 推演

静态 preflight 只做集合推演：

1. 从 manifest/初始 asset registry 得到可用逻辑资源集合
2. 按 `requested_stages` 和 `STAGE_ORDER` 遍历
3. 使用同一份 `requires` 判断缺失资源
4. stage 可运行时，把同一份 `produces` 加入集合
5. method 参数完整性使用 runtime 同一个 Pydantic schema/validator

### 4.4 Runtime 执行

runtime enter node 使用同一个 `requires` 调用严格 resolver，得到真实引用并验证存在性。

**区别：** preflight 与 runtime 的区别只是”静态可用性”与”真实引用”，不是两份业务规则。

### 4.5 需要删除的组件

- `_simulate_stage_outputs`
- `STAGE_OUTPUTS` 私有 key 模拟表
- 与 runtime 不同的 contextual-required 手写分支
- preflight 独有的依赖优先级
- 通过伪造输出让计划看似可运行的逻辑

### 4.6 Suggestion 处理

建议把人类可读 suggestion 作为错误码到文案的纯映射；它不能参与可运行性判断。

---

## 5. Universal Resolver 收窄

### 5.1 问题描述

`resolve_stage_resource` 的目标不是”尽量找到一个能用的东西”，而是解析架构已声明的当前资源。

### 5.2 允许的解析顺序

只允许以下三个来源，按顺序查找：

1. stage config 中显式且 contract 允许的 override
2. `state.resource_refs[resource_type]`
3. 初始 asset registry 中 contract 声明的 asset type

### 5.3 禁止的行为

- 扫描任意 stage output
- 猜 `final_*` key
- legacy key fallback
- 反向遍历 workflow
- 根据文件后缀决定资源语义
- registry JSON 损坏后返回空列表

### 5.4 错误处理

- **资源不存在**：asset registry 缺失文件可以返回”资源不存在”
- **数据损坏**：registry 文件存在但 JSON/schema 损坏必须抛出结构化错误，进入 terminal failure
- **原则**：数据损坏不能伪装成普通缺资源

---

## 6. 删除测试旁路

### 6.1 问题描述

生产执行路径不得识别”这是测试”。

### 6.2 需要删除的组件

- `has_explicit_path_override`
- stage output 的 `env.PATH` 特判
- “fake-binary tests” 注释和无 conda 时回退到 shell/PATH 的执行语义
- hifiasm/hifiasm_ont 中 “Fallback for tests” 参数来源
- 为旧 fixture 放宽 manifest、descriptor、resource 或 parameter 校验的 catch-all

### 6.3 统一执行边界

1. method descriptor + env config 解析出完整 `EnvSpec`
2. preflight 检查该 env 与 required tools
3. freeze 把明确的 env target 写入 `FrozenAttemptSpec`
4. runner 只按 frozen spec 执行，不重新选择 PATH/conda 策略

### 6.4 测试替身注入

测试替身只注入到边界：

- `preflight(..., runner=fake_runner)` 已有依赖注入能力，可以继续使用
- `LocalRunner` 增加显式 subprocess adapter/runner 注入，或在测试中 patch runner 边界
- 端到端测试使用临时 env config 和可控 runner，不把 PATH 写进业务 state
- TUI 测试从选择 method 和参数开始，断言产生的 run config 以及最终 frozen argv；不要只手工拼 stage output

---

## 7. P2 收敛项

### 7.1 LLM JSON 解析

**目标：** 把 response text 提取、markdown fence 去除、JSON object 校验收敛为一个 shared helper。

**规则：**
- domain 模块只负责各自 Pydantic schema/业务字段校验
- 优先使用模型 SDK 的 structured output
- 需要文本 fallback 时也只能调用同一个严格 parser
- 禁止每个 recommender 复制 `_response_text` 和 `_parse_json_object`
- 禁止 catch-all 后静默换另一套含义

**验收：** 生产代码只剩一个通用 JSON object parser，domain-specific normalize/validate 可以有多个。

### 7.2 Resume 协调

**目标：** `app/control/lifecycle.py` 应拥有一次完整 resume transaction。

**完整事务流程：**
1. 读取当前 checkpoint
2. 校验 session、review request、hash 和 decision
3. 必要时把外部 decision snapshot 到 run 内
4. 生成 state update
5. 写 decision/resume ledger event
6. `graph.update_state`
7. `graph.invoke(None, config)`
8. 返回新 state/result

**调用规则：**
- `run`、`resume`、interactive review 只负责输入输出和调用该服务，不各自复制这八步
- dry-run 只能停在第 3 步之前，不写 durable state

### 7.3 Graph、Lifecycle 与 Reports

**目标：** 固定依赖方向。

**依赖方向：**
```text
entrypoint -> control/lifecycle -> graph
entrypoint -> reports -> storage
graph -> stage builders
stage builders -> execution/storage
```

**规则：**
- graph builder 只组装 node/edge，不直接写 report
- blocked/failed/completed report 在 graph invoke 返回后由 lifecycle/entrypoint 触发
- route controller 的 ledger 逻辑抽成 lifecycle service；builder 只绑定该 node
- `trace_writer` 不再 import `review_report` 的 private function
- 删除 `review_report -> trace_writer -> review_report` 方向
- 最终 summary 只保留一个公开 writer；`write_final_review_report` 若只是转发则删除
- `_pending_confirmations` 等纯 projection helper 放入 `_projection.py` 或 trace projection 模块

**注意：** annotation 的现有大循环依赖不在本轮展开，但共享 reports/control 变更不得进一步加重它。

### 7.4 Ask 到 Run 的 Typed 调用

**目标：** 删除 `AskPlan -> 写文件 -> 转 argv -> _parse_args -> _run_session` 的内部回环。

**新流程：**
- TUI 仍可把 confirmed run config 持久化为审计 artifact
- 同时直接把已验证的 `RunConfig` 和 `EntryRequest` 交给公开的 `run_session()` service
- argparse 只存在于 CLI shell 边界
- `_run_session`、`_parse_args` 不应被其他业务模块当 API import

### 7.5 Durable JSON 与 Artifact Publication

**目标：** 区分两类写入。

**两类写入：**
1. **Immutable evidence/artifact**：只走 `app/storage/artifacts.py` 的 publish/register 边界
2. **Regenerable report**：只走 `_projection.atomic_write_text`

**标准化：**
- 提供一个 canonical JSON encoder（UTF-8、`ensure_ascii=False`、稳定排序/分隔符、结尾换行）
- 所有 hash 和持久化使用同一字节口径
- 删除 stage 中散落的 `write_text(json.dumps(...))` 作为 durable artifact 的做法
- 命令脚本和工具原生输出可以先写 work dir；collect 阶段再统一 publish

### 7.6 Manifest 加载

**目标：** 只保留一个严格 manifest loader。

**职责：**
- 负责 JSON/YAML 识别、相对路径锚定、Pydantic 校验和错误类型
- inventory、ask、run 都调用它
- ask 若允许”暂时没有 manifest”，应由调用方先判断 ref 是否存在
- loader 本身不能吞掉 parse/schema 错误并返回 `None`
- 文件存在但损坏时永远 fail closed

### 7.7 Path 与 Session Component 校验

**目标：** `SessionPaths` 是 session ID、stage component、artifact relative path 的唯一 containment authority。

**规则：**
- `EntryRequest` 调用同一 validator，不复制 regex/字符规则
- 所有 session 路径通过 `SessionPaths` 构造
- 删除入口、storage、report 各自不同的 sanitize 规则
- 用户展示 label 与文件系统 component 分开；展示文字不能直接拼路径

### 7.8 Genome Size、Evidence 与小 Wrapper

**目标：** 收敛重复的小工具函数。

**规则：**
- genome size 的解析、单位转换、来源优先级只保留在 `app/domains/genome_size.py`
- stage 只能请求 `resolve_genome_size`，不能重新解析字符串或另定优先级
- execution result、attempt evidence、artifact publication 的相同 helper 收敛到现有 execution/storage 模块
- method adapter 只提供 method-specific metadata
- 只转发参数且不增加 contract、错误语义或依赖隔离的 wrapper 直接删除
- `PlanWarning.model_dump_json_compat` 等兼容 API 直接删除
- read-QC 旧 output aliases、hifiasm test fallback、旧 route hints 全部删除，不做 deprecation 周期

---

## 8. 推荐修复顺序（已验证）

> ⚠️ **基于实际代码库验证**：本顺序已通过可执行性分析验证，采用渐进式、可回滚的策略。详见 `refactor-status-analysis.md`。

### 重构策略概述

采用 **4个Phase渐进式策略**，每个Phase独立验收，可回滚：

| Phase | 描述 | 时间 | 风险 | 破坏性 |
|-------|------|------|------|--------|
| Phase 1 | 建立基础设施 | 2-3周 | 低 | ❌ 无 |
| Phase 2 | 建立新架构（保留fallback） | 3-4周 | 中 | ❌ 无 |
| Phase 3 | 删除旧机制 | 4-5周 | 高 | ✅ 是 |
| Phase 4 | 清理和文档 | 1-2周 | 低 | ❌ 无 |
| **总计** | | **10-14周** | **中-高** | |

**关键原则：**
- Phase 1-2 不破坏现有功能，可持续集成
- Phase 2 建立新机制但保留旧逻辑作为 fallback
- Phase 3 开始删除前，新机制必须完全覆盖所有场景
- 每个Phase完成后打Git tag作为回滚点

---

### Phase 1：建立基础设施（2-3周，无破坏性）

**目标：** 建立统一的资源加载和配置管理，为后续重构奠定基础。

#### Commit 1.1：创建统一 Descriptor/Env Config Loader Facade

**任务：**
- 创建 `app/config.py` 或扩展现有文件
- 提供 `load_descriptor_registry()` 函数
- 提供 `load_env_config(path=None)` 函数
- 使用 `importlib.resources` 处理包资源
- 确保与 CWD 无关

**实现要点：**
```python
# app/config.py
from importlib import resources
from pathlib import Path

def load_descriptor_registry() -> DescriptorRegistry:
    “””Load descriptor registry from package resources.”””
    with resources.files('gene_agent').joinpath('descriptors/registry.yaml').open('rb') as f:
        return DescriptorRegistry.load(f)

def load_env_config(path: Path | None = None) -> EnvConfig:
    “””Load environment config from explicit path or default.”””
    if path:
        return EnvConfig.load(path)
    # Default env config handling
    ...
```

**验收标准：**
- [ ] 从 `/tmp` 运行 `gene-agent inspect --help` 成功
- [ ] 单元测试在临时 CWD 下通过
- [ ] 现有所有测试继续通过

**工作量：** 2-3天  
**风险：** 🟢 低

#### Commit 1.2：迁移所有调用点到新 Facade

**任务：**
- 替换30+个 `DescriptorRegistry.load(Path(“descriptors/registry.yaml”))` 调用
- 替换2个 `Path(“config/envs.yaml”)` 调用
- 更新 `pyproject.toml` 确保 package data 包含 descriptors
- 删除各调用点的默认路径常量

**受影响文件（已验证存在）：**
- 生产代码：10个文件（`app/stages/*/nodes.py`, `app/stages/*/method_application/*.py`）
- 测试代码：17个文件
- 入口点：`app/entrypoints/planner_catalog.py`

**验收标准：**
- [ ] `rg 'Path\(“descriptors/registry.yaml”\)' app` 返回0结果
- [ ] `rg 'Path\(“config/envs.yaml”\)' app` 返回0结果（测试除外）
- [ ] wheel 安装后 console script 可访问 descriptors
- [ ] 所有测试通过

**工作量：** 2-3天  
**风险：** 🟢 低

#### Commit 1.3：添加 Authority Invariant 测试

**任务：**
- 测试：workflow stage 集合与 graph builder mapping 完全一致
- 测试：descriptor key 等于 `method_name`
- 测试：method 的 `stage` 存在于 workflow
- 测试：每个 method 在 `by_stage` 中恰好出现一次
- 测试：内置资源加载与 CWD 无关

**实现示例：**
```python
def test_workflow_graph_consistency():
    “””Workflow STAGE_ORDER must match graph builder mapping.”””
    from app.workflow import STAGE_ORDER
    from app.graph.builder import STAGE_BUILDERS
    
    assert set(STAGE_ORDER) == set(STAGE_BUILDERS.keys())

def test_descriptor_registry_invariants():
    “””Descriptor registry must maintain key invariants.”””
    registry = load_descriptor_registry()
    
    for method_id, descriptor in registry.methods.items():
        # Key must equal method name
        assert method_id == descriptor.method_name
        # Stage must exist in workflow
        assert descriptor.stage in STAGE_ORDER
        # Method appears exactly once in by_stage
        assert sum(1 for m in registry.by_stage[descriptor.stage] if m == method_id) == 1
```

**验收标准：**
- [ ] 所有 invariant 测试通过
- [ ] 测试在 CI 中运行
- [ ] 测试文档化于 `tests/test_architecture_invariants.py`

**工作量：** 1-2天  
**风险：** 🟢 低

**Phase 1 里程碑：**
- ✅ 所有资源加载与 CWD 无关
- ✅ 架构不变量有自动化测试保护
- ✅ 为 Phase 2 准备好清晰的基础

---

### Phase 2：建立新架构（3-4周，渐进式）

**目标：** 建立 `resource_refs` 中央资源表，但保留旧逻辑作为 fallback，确保零破坏。

#### Commit 2.1：在 State 中添加 resource_refs（可选字段）

**任务：**
- 在 state schema 中添加 `resource_refs: dict[str, ResourceRef] = Field(default_factory=dict)`
- 定义 `ResourceRef` schema（包含 path、type、metadata）
- 不影响现有逻辑，字段为空时使用旧机制

**ResourceRef Schema 设计：**
```python
class ResourceRef(BaseModel):
    “””Reference to a workflow resource.”””
    resource_type: str  # long_reads, assembly, etc.
    paths: list[Path]  # One or more file paths
    source_stage: str | None = None  # Which stage produced it
    metadata: dict[str, Any] = Field(default_factory=dict)
```

**验收标准：**
- [ ] State schema 定义更新
- [ ] 空 `resource_refs` 不影响现有流程
- [ ] 单元测试覆盖 ResourceRef schema

**工作量：** 1天  
**风险：** 🟢 低

#### Commit 2.2：Inventory 填充 resource_refs

**任务：**
- inventory 将 manifest assets 注册到 `resource_refs`
- 同时保留旧的 asset registry
- 定义逻辑资源名映射：`raw_long_reads` → `long_reads`, `assembly_fasta_registered` → `assembly` 等

**实现要点：**
```python
def commit_inventory(...):
    # Existing logic
    state.asset_registry = ...
    
    # New logic - populate resource_refs
    state.resource_refs[“long_reads”] = ResourceRef(
        resource_type=”long_reads”,
        paths=manifest.raw_long_reads_paths,
        source_stage=”inventory”,
        metadata={“asset_type”: “raw_long_reads”}
    )
    
    if manifest.short_reads:
        state.resource_refs[“short_reads”] = ResourceRef(...)
    
    # ... other resources
```

**验收标准：**
- [ ] inventory 成功填充 `resource_refs`
- [ ] 现有 stage 继续使用旧机制正常运行
- [ ] 集成测试验证 resource_refs 内容正确

**工作量：** 1-2天  
**风险：** 🟢 低

#### Commit 2.3：逐个迁移 Stage 使用 resource_refs

**迁移顺序：** inventory → read_qc → genome_survey → assembly → assembly_dedup → assembly_review → hic_scaffolding → t2t

**每个 Stage 的迁移模式：**
```python
def enter_stage(...):
    # Try new mechanism first
    long_reads = state.resource_refs.get(“long_reads”)
    
    # Fallback to old mechanism
    if not long_reads:
        long_reads = resolve_stage_resource(
            state, 
            ResourceSpec(resource_type=”raw_reads”, ...)
        )
    
    # Use long_reads for processing
    ...

def commit_stage(...):
    # Update resource_refs with new output
    state.resource_refs[“assembly”] = ResourceRef(
        resource_type=”assembly”,
        paths=[assembly_path],
        source_stage=”assembly”,
        metadata={...}
    )
    
    # Also update old mechanism for compatibility
    state.stage_outputs[“assembly”] = {
        “draft_assembly_ref”: assembly_path,
        ...
    }
```

**每个 Stage 的验收标准：**
- [ ] Stage 优先读取 `resource_refs`
- [ ] Fallback 到旧机制时记录 warning
- [ ] Stage commit 更新 `resource_refs`
- [ ] 该 stage 的所有测试通过
- [ ] 端到端测试验证资源正确传递

**工作量：** 每个 stage 1-1.5天，总计 8-12天  
**风险：** 🟡 中（每个 stage 独立，可增量验证）

**特殊注意事项：**
- **read_qc：** 过滤后的 reads 替换 `long_reads`
- **assembly：** 产出初始 `assembly` 资源
- **assembly_dedup/review：** 依次更新同一个 `assembly` 资源
- **hic_scaffolding/t2t：** 继续更新 `assembly`，形成最终版本

#### Commit 2.4：Preflight 改用 resource_refs

**任务：**
- preflight 优先使用 `resource_refs` 做集合推演
- 保留对 asset registry 的 fallback
- 使用与 runtime 相同的 `requires`/`produces` 定义

**实现要点：**
```python
def preflight_check(...):
    # Start with resources from manifest
    available_resources = set(state.resource_refs.keys())
    
    # Or fallback to asset types
    if not available_resources:
        available_resources = infer_from_asset_registry(state)
    
    # Iterate through requested stages
    for stage_name in requested_stages:
        contract = STAGE_CONTRACTS[stage_name]
        
        # Check required resources
        missing = contract.requires - available_resources
        if missing:
            return PreflightFailure(f”Missing: {missing}”)
        
        # Add produced resources
        available_resources.update(contract.produces)
    
    return PreflightSuccess()
```

**验收标准：**
- [ ] Preflight 使用 `resource_refs` 推演
- [ ] Preflight 与 runtime 使用相同错误码
- [ ] 所有 preflight 测试通过

**工作量：** 2-3天  
**风险：** 🟡 中

**Phase 2 里程碑：**
- ✅ `resource_refs` 机制完全建立
- ✅ 所有主线 stage 已迁移
- ✅ 旧机制仍然可用作 fallback
- ✅ 零破坏性变更，可持续集成

---

### Phase 3：删除旧机制（4-5周，破坏性）

> 🔴 **警告：** Phase 3 开始前必须确认 Phase 2 完全完成，所有测试通过，且在生产环境验证新机制稳定。

**前置条件检查清单：**
- [ ] 所有 stage 已迁移到 `resource_refs`
- [ ] Preflight 已切换到新机制
- [ ] 端到端测试矩阵全部通过
- [ ] 在 staging 环境运行至少1周无问题
- [ ] 团队评审并批准进入 Phase 3

#### Commit 3.1：删除 prefer_stages 和 output_keys

**任务：**
- 从 `ResourceSpec` 删除 `prefer_stages` 和 `output_keys` 字段
- 从 `resolve_stage_resource` 删除旧扫描逻辑
- 删除 `final_*_for_downstream` 模式
- `resolve_stage_resource` 只查：override → `resource_refs` → asset registry

**受影响文件（已验证）：**
- `app/stages/base.py`
- `app/stages/resource_specs.py`
- `app/stages/assembly_review/nodes.py`

**验收标准：**
- [ ] `rg “prefer_stages|output_keys” app` 返回0结果
- [ ] `rg “final_.*_for_downstream” app` 返回0结果
- [ ] 所有 stage 测试通过
- [ ] 资源缺失时给出清晰错误消息

**工作量：** 2-3天  
**风险：** 🟡 中（依赖 Phase 2 完成质量）

#### Commit 3.2：统一执行授权到 execution_mode

**任务：**
- 定义 `ExecutionMode = Literal[“auto”, “execute”, “plan_only”]`
- 在 `RunConfig` 添加 `execution_mode: ExecutionMode = “auto”` 字段
- CLI 添加 `--execution-mode` 选项（替代 `--local-exec`）
- 实现统一的 `should_execute_stage()` 函数
- 删除所有 stage 中的 `local_execution_not_enabled` 分支
- 删除 `validate_local_assembly_execution` 函数
- 测试中 `execution_mode=”local”` 改为 `execution_mode=”execute”`（简单替换）

**受影响文件（已验证）：**
- `app/schemas/run_config.py` - 添加 ExecutionMode，弃用 local_exec
- `app/entrypoints/run.py` - 更新 CLI 参数
- `app/stages/read_qc/nodes.py` (2处)
- `app/stages/genome_survey/nodes.py` (2处)
- `app/stages/assembly/nodes.py` (4处)
- `app/stages/read_qc/routing.py`
- 50+个测试文件（简单的字符串替换）

**统一的执行语义：**
- `execution_mode=”auto”` - 根据命令判断（run=执行，plan=不执行）
- `execution_mode=”execute”` - 强制执行（用于测试）
- `execution_mode=”plan_only”` - 只规划不执行
- `requested_stages` 决定 workflow 路径
- `planner_only` method 不能进入可执行 run config

**测试迁移策略：**
- 全局替换：`execution_mode=”local”` → `execution_mode=”execute”`
- 无需重写测试逻辑，只是参数值变化
- 保持现有的 mock 和 fake 机制

**验收标准：**
- [ ] `ExecutionMode` 类型定义完成
- [ ] 所有 stage 使用统一的 `should_execute_stage()`
- [ ] 测试中 `execution_mode=”local”` 已全部替换
- [ ] `rg “local_execution_not_enabled” app` 返回0结果
- [ ] 旧 run config 带 `local_exec` 时给出迁移提示

**工作量：** 2-3天（从原计划的5-7天降低）  
**风险：** 🟡 中（测试影响小）

#### Commit 3.3：删除 AssemblySelection 和 Runtime 方法选择

**任务：**
- TUI 必须输出 canonical method ID 和完整参数
- 删除 `AssemblySelection` 类
- 删除 `assembly_route` 字段
- 删除 `assembly_selection` 字段
- 删除 `DEFAULT_PRIMARY_BY_READ_TYPE` 常量
- Runtime 只做 method/read compatibility 校验，不做选择
- Descriptor `planner.parameter_defaults` 成为唯一默认值来源

**受影响文件（已验证）：**
- `app/domains/assembly/selection.py` (整个文件)
- `app/schemas/run_config.py`
- 12个使用 `assembly_selection` 的文件

**新的方法选择流程：**
```
User Input (read type, preferences)
    ↓
TUI reads descriptor registry
    ↓
TUI presents compatible methods + parameter_defaults
    ↓
User confirms method + parameters
    ↓
TUI writes run config with canonical method ID
    ↓
Runtime validates compatibility
    ↓
Runtime executes (no selection, no defaults)
```

**验收标准：**
- [ ] `rg “AssemblySelection|assembly_route|assembly_selection” app` 返回0结果
- [ ] `rg “DEFAULT_PRIMARY_BY_READ_TYPE” app` 返回0结果
- [ ] TUI 测试覆盖 ONT/HiFi/CLR 各一条完整流程
- [ ] Runtime 不补默认值，参数缺失时明确失败
- [ ] 旧 run config 带 `assembly_route` 时明确拒绝

**工作量：** 5-7天  
**风险：** 🔴 高（核心选择逻辑重构）

#### Commit 3.4：删除测试旁路

**任务：**
- 删除 `has_explicit_path_override` 函数
- 删除 stage output 的 `env.PATH` 特判
- 删除 “Fallback for tests” 参数来源
- 统一执行边界：descriptor + env config → EnvSpec → frozen spec → runner
- 测试通过显式 runner 依赖注入

**受影响文件（已验证）：**
- `app/execution/env_dispatch.py`
- `app/stages/assembly/method_application/hifiasm.py`
- `app/stages/assembly/method_application/hifiasm_ont.py`

**测试改造策略：**
```python
# Before
def test_assembly(tmp_path):
    result = run_assembly(..., execution_mode=”local”)

# After
def test_assembly(tmp_path, fake_runner):
    result = run_assembly(..., runner=fake_runner)
```

**验收标准：**
- [ ] `rg “has_explicit_path_override” app` 返回0结果
- [ ] `rg “Fallback for tests|fake-binary” app` 返回0结果
- [ ] 生产代码不识别”这是测试”
- [ ] 所有测试通过 runner 注入

**工作量：** 3-5天  
**风险：** 🟡 中

**Phase 3 里程碑：**
- ✅ 所有兼容层和旧机制已删除
- ✅ 代码库符合”唯一权威”原则
- ✅ 通过手册第10节的删除完成检查

---

### Phase 4：清理和文档（1-2周）

**目标：** 收敛重复代码，更新文档，确保长期可维护性。

#### Commit 4.1：P2 收敛项

**4.1.1 LLM JSON 解析收敛**
- 统一 response text 提取、markdown fence 去除、JSON object 校验
- 删除各 recommender 中的重复 `_response_text` 和 `_parse_json_object`
- 验收：生产代码只剩一个通用 JSON parser

**4.1.2 Resume 协调收敛**
- `app/control/lifecycle.py` 拥有完整 resume transaction
- `run`、`resume`、interactive review 只调用该服务
- 验收：CLI resume 与 interactive resume 产生同构 ledger

**4.1.3 Graph/Lifecycle/Reports 依赖方向修正**
- 固定依赖：entrypoint → lifecycle → graph, entrypoint → reports → storage
- graph builder 不直接写 report
- 删除循环依赖
- 验收：import cycle 检查通过

**4.1.4 Ask → Run Typed 调用**
- 删除 `AskPlan → 写文件 → 转 argv → _parse_args` 回环
- TUI 直接调用 `run_session(RunConfig, EntryRequest)`
- 验收：argparse 只在 CLI 边界

**4.1.5 其他小项**
- Durable JSON 与 artifact publication 标准化
- Manifest 加载收敛
- Path 与 session component 校验统一
- Genome size、evidence helper 收敛
- 删除 `model_dump_json_compat` 等兼容 API

**工作量：** 4-6天  
**风险：** 🟡 中（可并行）

#### Commit 4.2：文档归零后重写

**文档更新清单：**

| 文档 | 更新内容 |
|------|---------|
| `README.md` | 真实 CLI、运行授权、TUI 到 run 的流程 |
| `docs/architecture.md` | 唯一 authority、state/resources、control/graph/report 依赖方向 |
| `docs/stage_contracts.md` | 每个 stage 的 requires/produces/method/review contract |
| `docs/descriptor_spec.md` | planner defaults 与 runtime constraints 的边界 |
| `docs/examples/*.yaml` | 移除旧字段，全部由当前 schema 验证 |
| `docs/beginner_mainline_guide.md` | 移到 archive 或标注历史 |
| `docs/phases/**` | 统一标注 superseded |

**原则：**
- 不逐句打补丁，按目标架构重写
- 当前文档对同一事实不能有不同口径
- 所有示例必须能被当前代码实际运行

**验收标准：**
- [ ] 所有 example config 加载成功
- [ ] 文档与代码一致性检查通过
- [ ] 新用户可按文档完成首次运行

**工作量：** 2-3天  
**风险：** 🟢 低

**Phase 4 里程碑：**
- ✅ 代码库无重复逻辑
- ✅ 文档完全更新
- ✅ 可维护性达到最优

---

## 9. 关键测试矩阵

### 9.1 TUI 到执行

| 场景 | 应验证 |
|------|--------|
| ONT + 用户选 hifiasm_ont | run config 只有 canonical method；参数完整；frozen argv 完全一致 |
| ONT + 用户选 Flye | 不被 runtime 改选；兼容性通过后按 Flye 执行 |
| HiFi + 用户选 hifiasm | TUI 填入确认后的 threads；runtime 不补默认 |
| method 缺参数 | TUI 不允许确认；直接 run config 也严格失败 |
| method/read type 不兼容 | runtime 校验失败，不 fallback 到另一工具 |
| 带 `assembly_route`/`assembly_selection` | schema 明确拒绝 |

### 9.2 资源交接

| 场景 | 应验证 |
|------|--------|
| manifest raw long reads -> assembly | assembly 读取 `resource_refs.long_reads` |
| read_qc filter -> genome survey/assembly | 两个 stage 得到同一 canonical read refs |
| assembly -> dedup -> review | 每次只替换 `resource_refs.assembly` |
| review -> Hi-C -> T2T | 下游不读取任何 `final_*` 私有 key |
| asset registry 损坏 | terminal structured failure，不当作资源缺失 |
| 缺少 required resource | preflight 与 runtime 使用同一错误码 |

### 9.3 环境与路径

| 场景 | 应验证 |
|------|--------|
| 从临时 CWD 加载 registry | 成功 |
| 从临时 CWD 加载显式 env config | 成功，relative root 相对 config |
| FrozenAttemptSpec 无 env target | freeze 校验失败 |
| 测试 fake runner | 通过注入工作，state 中没有 PATH 特判 |
| wheel 安装后运行 console script | descriptor package data 可用 |

### 9.4 Resume 与 Reports

| 场景 | 应验证 |
|------|--------|
| CLI resume 与 interactive resume | 调用同一 transaction，产生同构 ledger/state |
| dry-run review | 不写 decision artifact、ledger、checkpoint |
| blocked/failed/completed | graph 返回后由同一 report service 生成投影 |
| 重生成 report | 不改变 ledger/artifact index |

---

## 10. 删除完成检查（验收清单）

### 10.1 自动化搜索检查

重构结束后，下面搜索在生产代码中应为零，或只出现在明确标记为历史的 archive 文档中：

```bash
# 执行授权相关（统一性检查）
rg -n “local_execution_not_enabled” app  # 应为0，已统一到 execution_mode
rg -n “validate_local_assembly_execution” app  # 应为0，已统一

# 方法选择相关
rg -n “assembly_route|assembly_selection|AssemblySelection|DEFAULT_PRIMARY_BY_READ_TYPE” app tests README.md docs

# 路径问题
rg -n 'Path\(“descriptors/registry\.yaml”\)|Path\(“config/envs\.yaml”\)' app

# 测试旁路
rg -n “has_explicit_path_override|fake-binary|Fallback for tests|fallback for tests” app

# 资源交接旧机制
rg -n “final_.*_for_downstream|prefer_stages|output_keys” app

# 兼容 API
rg -n “model_dump_json_compat” app tests
```

**预期结果：** 所有搜索返回0个结果（archive 文档除外）

### 10.2 Fallback 审查

允许保留的 `fallback` 必须满足：
- ✅ 是明确的业务策略
- ✅ 能指出唯一 contract
- ❌ 不是兼容旧字段
- ❌ 不是吞掉数据损坏
- ❌ 不是照顾测试 fixture

**审查方法：**
```bash
rg -n “fallback” app --type py
```
对每个结果进行人工审查，确保符合上述标准。

### 10.3 最终验收清单（Phase 3 完成后）

#### 基础设施验收

- [ ] **CWD 独立性**
  ```bash
  cd /tmp
  gene-agent inspect --help
  gene-agent run --manifest /absolute/path/to/manifest.yaml --run-config /absolute/path/to/run.json
  ```

- [ ] **Package Data 完整性**
  ```bash
  pip install -e .
  python -c “from app.config import load_descriptor_registry; load_descriptor_registry()”
  ```

- [ ] **Authority Invariants**
  ```bash
  pytest tests/test_architecture_invariants.py -v
  ```

#### 功能验收

- [ ] **全量测试通过**
  ```bash
  pytest tests/ -v
  # 预期：所有测试通过，无 skip，无 xfail
  ```

- [ ] **端到端测试矩阵通过**（见第9节）
  - [ ] ONT + hifiasm_ont 流程
  - [ ] HiFi + hifiasm 流程
  - [ ] CLR + Flye 流程
  - [ ] Assembly → Dedup → Review → Hi-C → T2T 资源传递
  - [ ] Preflight 与 runtime 错误码一致性

- [ ] **Example Config 验证**
  ```bash
  for f in docs/examples/*.yaml docs/examples/*.json; do
    gene-agent validate-config “$f” || echo “FAILED: $f”
  done
  ```

#### 架构验收

- [ ] **Import Cycle 检查**
  ```bash
  python -c “
  import sys
  from importlib import import_module
  
  # Check no circular imports
  import_module('app.graph.builder')
  import_module('app.control.lifecycle')
  import_module('app.reports.review_report')
  
  # Check dependency direction
  from app.graph import builder
  assert 'entrypoints' not in str(builder.__file__)
  assert 'reports' not in str(builder.__file__)
  print('✓ Dependency direction correct')
  “
  ```

- [ ] **Schema 严格性验证**
  ```bash
  # 旧 run config 应明确失败
  cat > /tmp/old_config.json << 'EOF'
  {
    “manifest_ref”: “manifest.yaml”,
    “local_exec”: [“assembly”],
    “assembly_route”: “auto”,
    “requested_stages”: [“assembly”]
  }
  EOF
  
  gene-agent run --run-config /tmp/old_config.json 2>&1 | grep -q “extra fields not permitted”
  echo “✓ Old config correctly rejected”
  ```

#### 文档验收

- [ ] **文档一致性检查**
  - [ ] README.md 中的 CLI 示例能实际运行
  - [ ] docs/architecture.md 中的架构图与代码一致
  - [ ] docs/stage_contracts.md 列出的 requires/produces 与代码一致
  - [ ] docs/examples/ 中的所有示例能加载成功

- [ ] **无冲突口径**
  ```bash
  # 手动检查：authority、resource handoff、method selection 等概念
  # 在 README.md, docs/architecture.md, docs/stage_contracts.md 中描述一致
  ```

#### 质量验收

- [ ] **代码质量**
  ```bash
  # No trailing whitespace, no mixed line endings
  git diff --check
  
  # Linting passes
  ruff check app/
  mypy app/
  ```

- [ ] **性能回归检查**
  - [ ] 运行 benchmark suite（如存在）
  - [ ] 确认无显著性能下降（>10%）

### 10.4 Phase 门控检查

每个 Phase 完成必须通过对应检查才能进入下一 Phase：

#### Phase 1 → Phase 2 门控

- [ ] 所有 descriptor/env config 加载使用 facade
- [ ] 从 `/tmp` 运行成功
- [ ] Authority invariant 测试通过
- [ ] 所有现有测试继续通过
- [ ] Code review 批准

#### Phase 2 → Phase 3 门控

- [ ] 所有8个 stage 已迁移到 `resource_refs`
- [ ] Preflight 使用 `resource_refs`
- [ ] 端到端测试验证资源正确传递
- [ ] 在 staging 环境运行至少1周
- [ ] 性能无回归
- [ ] 团队评审批准进入破坏性变更阶段

#### Phase 3 → Phase 4 门控

- [ ] 所有旧机制已删除（通过 10.1 搜索检查）
- [ ] 所有测试已迁移并通过
- [ ] 旧 config 正确拒绝并给出清晰错误
- [ ] 生产环境烟雾测试通过
- [ ] 无 P0/P1 bug

#### Phase 4 完成门控

- [ ] 所有文档更新完成
- [ ] Example configs 验证通过
- [ ] 最终验收清单（10.3）全部通过
- [ ] Release notes 完成
- [ ] 迁移指南完成

---

## 11. 风险管理和回滚策略

### 11.1 风险等级定义

| 等级 | 定义 | 影响 | 回滚时间 |
|------|------|------|---------|
| 🟢 低 | 局部变更，充分测试覆盖 | 单一组件 | < 1小时 |
| 🟡 中 | 跨组件变更，可能影响多个功能 | 多个组件 | 1-4小时 |
| 🔴 高 | 架构级变更，广泛影响 | 整个系统 | 4-24小时 |

### 11.2 各 Phase 风险评估

| Phase | 风险等级 | 主要风险 | 缓解措施 |
|-------|---------|---------|---------|
| Phase 1 | 🟢 低 | CWD 处理不当导致资源找不到 | 充分的路径测试，在多种环境验证 |
| Phase 2 | 🟡 中 | resource_refs 逻辑错误导致资源传递失败 | 保留 fallback，增量迁移每个 stage |
| Phase 3 | 🔴 高 | 删除旧机制后发现遗漏场景 | Phase 2 充分验证，staging 环境长期运行 |
| Phase 4 | 🟢 低 | 文档不同步 | 自动化文档验证，示例实际运行 |

### 11.3 回滚计划

#### Git Tag 策略

每个 Phase 完成后打 tag：

```bash
# Phase 1 完成
git tag -a v-refactor-phase1-complete -m “Phase 1: Infrastructure complete”

# Phase 2 完成
git tag -a v-refactor-phase2-complete -m “Phase 2: resource_refs established”

# Phase 3 完成
git tag -a v-refactor-phase3-complete -m “Phase 3: Old mechanisms removed”

# Phase 4 完成（最终版本）
git tag -a v-refactor-complete -m “Refactor complete”
```

#### 回滚触发条件

- **P0 Bug：** 核心功能完全不可用
- **P1 Bug：** 主要功能严重受损，无 workaround
- **性能回归：** > 50% 性能下降
- **数据损坏：** 任何导致 state 损坏的 bug

#### 回滚步骤

1. **评估影响**
   ```bash
   # 确定出问题的 Phase
   git log --oneline --decorate
   ```

2. **回滚到上一个 Phase**
   ```bash
   # 例如从 Phase 3 回滚到 Phase 2
   git reset --hard v-refactor-phase2-complete
   ```

3. **验证回滚**
   ```bash
   pytest tests/ -v
   # 在 staging 环境运行烟雾测试
   ```

4. **分析根因**
   - 提取失败日志
   - 确定遗漏的场景
   - 更新测试覆盖

5. **修复后重试**
   - 在分支上修复问题
   - 充分测试
   - 重新进入该 Phase

### 11.4 监控和观察

#### Phase 2 监控指标

- **Resource refs 使用率：** 每个 stage 使用 `resource_refs` vs fallback 的比例
- **Fallback 频率：** 记录 fallback 到旧机制的次数和原因
- **资源传递错误：** 资源找不到或格式错误的次数

#### Phase 3 监控指标

- **旧 config 拒绝率：** 多少用户尝试使用旧 config
- **错误消息质量：** 用户是否能理解错误消息并自行修复
- **测试执行时间：** 确认删除旧机制后测试变快而非变慢

### 11.5 应急预案

如果 Phase 3 完成后发现无法回滚的严重问题：

1. **热修复路径**
   - 在当前代码基础上紧急修复
   - 跳过正常 review 流程（保留事后 review）
   - 优先恢复功能，清理代码延后

2. **通信计划**
   - 通知所有受影响用户
   - 提供 workaround
   - 设置修复 ETA

3. **事后复盘**
   - 分析为何测试未捕获该问题
   - 更新测试覆盖
   - 记录 lessons learned

---

## 12. 完成定义

这轮重构完成时，主线应满足以下一句话描述：

> **TUI 依据 descriptor 给出选择和默认值，确认后生成完整 run config；workflow 按唯一 stage 顺序推进，stage 从 canonical resource table 取得输入，按明确 method 和参数冻结一次执行，runner 只执行 frozen spec，storage 保存证据，lifecycle 负责 resume，reports 只从 durable facts 生成投影。**

### 完成标准详细定义

#### 唯一权威（Single Source of Truth）

- ✅ 每类事实只有一个定义位置
- ✅ Workflow stage 顺序：`app/workflow.py`
- ✅ Method 元数据：`descriptors/registry.yaml`
- ✅ Stage 资源 contract：`app/stages/resource_specs.py`
- ✅ 参数默认值：descriptor `planner.parameter_defaults`
- ✅ 工具环境：env config

#### 清晰的执行边界

- ✅ TUI 负责选择和默认值，输出完整 run config
- ✅ Runtime 只执行，不选择，不补默认值
- ✅ Method 被选中 = 总是执行，无 `local_exec` 开关
- ✅ Frozen spec 完全确定，runner 不做任何决策

#### 中央资源管理

- ✅ 跨 stage 通过 `resource_refs` 交接
- ✅ 逻辑资源名明确定义
- ✅ 资源缺失时给出清晰错误，不猜测
- ✅ 资源来源可追溯

#### 测试清晰度

- ✅ 生产代码不识别”这是测试”
- ✅ 测试通过依赖注入而非特判
- ✅ 测试验证目标架构，不为旧逻辑保留旁路

#### 路径管理

- ✅ 内置资源与 CWD 无关
- ✅ 可从任意目录运行
- ✅ Package 安装后功能完整

### 负面验收（不应该出现的行为）

任何还需要以下行为的代码，都说明对应修理项尚未完成：

- ❌ “再看一个 route hint”
- ❌ “再扫一个旧 key”
- ❌ “没有值就按测试默认”
- ❌ “换个 CWD 才能找到配置”
- ❌ “这个 stage 特殊，不需要明确选择”
- ❌ “runtime 根据 read type 自动选方法”
- ❌ “先试这个 key，不行再试那个”
- ❌ “损坏了就返回空，假装不存在”

---

## 13. 总结和下一步

### 13.1 重构价值

完成本次重构后，系统将获得：

1. **可理解性提升** - 每类事实只有一个定义位置，易于查找和理解
2. **可测试性提升** - 依赖注入取代特判，测试边界清晰
3. **可维护性提升** - 删除重复逻辑，减少维护负担
4. **可扩展性提升** - 清晰的 contract 使新 stage 集成更简单
5. **可靠性提升** - 明确失败取代静默转换，问题易于诊断

### 13.2 工作量总结

| Phase | 时间估算 | 人员需求 | 并行可能性 |
|-------|---------|---------|-----------|
| Phase 1 | 2-3周 | 1-2人 | 低（基础设施必须先完成） |
| Phase 2 | 3-4周 | 2-3人 | 中（stage 迁移可并行） |
| Phase 3 | 4-5周 | 2-3人 | 中（不同删除任务可并行） |
| Phase 4 | 1-2周 | 1-2人 | 高（文档和代码清理可并行） |
| **总计** | **10-14周** | **2-3人** | |

### 13.3 立即行动项

1. ✅ **已完成** - 可执行性分析报告
2. ✅ **已完成** - 重构手册更新
3. 📋 **待完成** - 补充需求文档
4. 📋 **待完成** - 关键决策讨论文档
5. 📋 **待启动** - Phase 1 Commit 1.1

### 13.4 成功因素

- **充分的测试覆盖** - 在每个 Phase 门控处严格验证
- **渐进式策略** - Phase 1-2 不破坏现有功能
- **团队共识** - 关键决策需要全员理解和支持
- **风险意识** - 识别高风险项并制定应急预案
- **文档同步** - 代码和文档始终保持一致

### 13.5 长期维护

重构完成后，需要持续维护以下约束：

1. **Authority Invariant 测试** - 在 CI 中持续运行
2. **Schema 严格性** - 不重新引入兼容层
3. **依赖方向** - 定期检查 import cycle
4. **文档更新** - 代码变更必须同步更新文档
5. **Code Review** - 拒绝引入旧模式的 PR

---

## 附录 A：快速参考

### A.1 核心原则速查

1. **唯一权威**：每类事实只有一个所有者
2. **明确失败**：旧配置不兼容，不静默转换
3. **显式参数**：运行时不发明默认值
4. **中央资源**：跨 stage 通过 `resource_refs` 交接
5. **测试注入**：生产代码不识别”这是测试”

### A.2 关键删除项速查

| 类别 | 删除项 |
|------|--------|
| 执行授权 | `local_exec`, `LocalExecStage`, `--local-exec` |
| 方法选择 | `AssemblySelection`, `assembly_route`, `DEFAULT_PRIMARY_BY_READ_TYPE` |
| 资源交接 | `prefer_stages`, `output_keys`, `final_*_for_downstream` |
| 测试旁路 | `has_explicit_path_override`, fake-binary fallback, PATH 特判 |
| 兼容层 | `model_dump_json_compat`, 旧 output aliases |

### A.3 权威分配速查

| 事实 | 唯一权威 |
|------|----------|
| Stage 定义 | `app/workflow.py` |
| Method 元数据 | `descriptors/registry.yaml` |
| 资源 contract | `app/stages/resource_specs.py` |
| 默认参数 | descriptor `planner.parameter_defaults` |
| 工具环境 | env config |

---

## 附录 B：术语表

- **Authority（权威）**：某类信息的唯一定义来源
- **Canonical（规范）**：标准化的、唯一的表示形式
- **Contract（契约）**：stage 输入输出的正式约定
- **FOFN**：File of Filenames，文件名列表文件
- **Frozen spec**：已确定所有参数的不可变执行规范
- **Preflight**：执行前的静态可行性检查
- **Resource handoff**：跨 stage 的资源传递机制
- **Shim（垫片）**：兼容层代码
- **TUI**：Terminal User Interface，终端用户界面
