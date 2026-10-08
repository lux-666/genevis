<a id="method-execution"></a>

# 方法执行

> **状态：** 已实现
>
> **阅读场景：** 追踪选定方法如何成为持久进程尝试，或诊断冻结、执行、收集和恢复。
>
> **来源：** [TUI 与规划](tui-and-planning.md)、[运行时编排](runtime-orchestration.md)，以及适用时的[参数推荐](parameter-recommendation.md)。
>
> **下游：** [数据与存储](data-and-storage.md)、阶段收集、[评审与恢复](review-and-resume.md)、[AutoHiC](autohic.md)等适配器。
>
> **依赖契约：** [方法执行生命周期](../reference/execution-lifecycle.md)、[执行证据](../reference/execution-evidence.md)、[运行数据](../reference/data-contract.md)、[资源交接](../reference/resource-handoff.md)。
>
> **代码依据：** `app/storage/frozen_inputs.py`、`app/execution/dependency_snapshot.py`、`app/execution/lifecycle.py`、`app/execution/attempt_owners.py`、`app/execution/method_runner.py`、`app/execution/local_runner.py`、`app/execution/evidence.py`、`app/execution/__init__.py`、`tests/test_execution_lifecycle.py`。

<a id="two-freeze-boundaries"></a>

## 两个冻结边界

Gene Agent 在不同范围内冻结运行两次。将二者视为一次操作会破坏来源和恢复语义。

| 边界 | 发生时机 | 所有内容 | 是否重复 |
|---|---|---|---|
| 整次运行输入冻结 | 操作者确认 TUI 计划、父图启动前 | `control/` 下的规范输入 manifest、运行配置和机器依赖快照 | 每个会话一次；同一请求幂等，不同请求冲突 |
| 方法尝试冻结 | 阶段选定方法并完成必需推荐与审批 | 一份精确保留脚本、工作目录、环境投影、预期输出和类型化输入依赖 | 每次尝试一次；一次运行可有多个方法、批次和重试 |

整次运行冻结防止执行会话跟随输入草稿或 `config/envs.yaml` 的后来修改。方法尝试冻结防止恢复依据后来代码、目录、配置或环境重建命令。因此重试创建新的方法尝试，不修改会话控制快照或早先冻结尝试。

最终确认不会立即冻结。TTY 检查后，只读的运行专属就绪检查探测规范有效本地方法闭包及必需 LLM 端点；整次运行依赖快照使用同一闭包，失败时不创建会话。更广的机器目录就绪检查由 [bootstrap](bootstrap.md)负责；执行仍在实际使用冻结依赖的边界验证选择。活动映射可人工维护，bootstrap 只是可选的配置目录验证助手。

<a id="shared-method-flow"></a>

## 共享方法流程

所有生产方法适配器在选择和可选审批后接入同一生命周期：

```text
阶段适配器准备精确尝试
  -> 共享 freeze 持久化并绑定所有者游标
  -> 共享 run 验证身份、脚本、路径和实时输入依赖
  -> Runner 至多启动一个待执行尝试
  -> 共享代码发布日志和持久执行证据
  -> 阶段适配器收集生物学输出
  -> 阶段路由评审、重试、提交或失败
```

执行内核拥有进程调用、日志发布、证据持久化、恢复查找和进程失败策略。适配器拥有冻结前的命令构建以及运行后的生物学验证/发布。适配器不得建立私有 Runner 路径或另一套执行证据 schema。精确状态转换和失败策略见[生命周期契约](../reference/execution-lifecycle.md)。

<a id="the-production-runner-is-local"></a>

## 生产 Runner 是本地的

`Runner` 是小型依赖注入协议。生产调用不传测试覆盖，因此实例化 `LocalRunner`，在声明的工作目录用 `subprocess.run` 执行冻结脚本。有 Conda 时直接以 `conda run ... <interpreter> <script_path>` 形式表达；无 Conda 时 argv 以冻结解释器和脚本路径开头。生产调用始终使用 `shell=False`。非空冻结 `env_setup` 会被拒绝，不会求值或拼接进 shell 命令。

“本地”指运行 `gene-agent` 进程的主机。真正的服务器运行是在服务器检出目录启动同一 CLI。没有生产 SSH Runner、调度器提交后端、远程代理传输或进程重新附着实现。`FakeRunner` 仅供测试，不是第二种生产执行模式。

<a id="runtime-result-versus-durable-evidence"></a>

## 运行时结果与持久证据

`ExecutionResult` 与 `ExecutionEvidence` 有意使用不同类型。

- `ExecutionResult` 是即时进程值，携带本地 stdout、stderr 和预期输出路径，供共享发布者检查和移动文件。
- `ExecutionEvidence` 是无路径的持久进程正文，包含结果及两个日志的已验证产物引用；外层不可变执行记录另行绑定精确冻结尝试引用。

“无路径”指没有裸主机本地 `Path` 字段；每个 `ArtifactRef` 仍包含已验证的运行相对 `path_ref`。生物学输出仍由阶段拥有，预期输出路径属于冻结尝试，不属于 `ExecutionEvidence`。

运行时模型绝不转储到 checkpoint。只有 stdout/stderr 发布并索引后才转换；精确 schema 和完整性规则见[执行证据契约](../reference/execution-evidence.md)。

<a id="attempt-ownership-and-resume"></a>

## 尝试所有权与恢复

普通一次性方法和有序批次使用直接尝试游标。参数化 Assembly、AutoHiC、T2T 方法将冻结尝试和执行引用绑定到当前 `ParameterAttempt`。两类所有者都使用同一运行内核；区别只是紧凑恢复引用的存放位置，不是进程运行方式。

进入时，冻结游标从 run 恢复，ran 游标从 collect 恢复。启动进程前，run 节点在产物索引中查找绑定同一方法、尝试和冻结输入的唯一执行记录。已验证记录使游标前进而不重跑；缺失、歧义或不匹配证据必须拒绝。有序直接批次每次图转换推进一个项目，并 checkpoint 已完成前缀。

这只是持久证据提交后的回放保护，不是进程级 exactly-once 执行。子进程退出但执行记录尚未提交时发生机器故障，无法关联远程调度器身份，因为当前 Runner 没有该身份。不得把此限制描述为已实现的重新附着。
