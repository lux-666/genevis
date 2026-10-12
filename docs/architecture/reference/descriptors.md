<a id="descriptor-contract"></a>

# 描述符契约

> **状态：** 已实现。
>
> **阅读场景：** 检查软件包能力、方法身份、必需输入、预期输出或规划元数据。
>
> **来源：** 规划、bootstrap 和阶段方法选择。
>
> **下游：** [执行](../topics/execution.md)、[参数推荐](../topics/parameter-recommendation.md)。
>
> **相关契约：** [阶段契约](stage-contracts.md)、[治理](governance.md)。
>
> **代码依据：** `descriptors/registry.yaml`、`app/schemas/descriptors.py`、`app/configuration.py`、`app/entrypoints/planner_catalog.py`、`tests/test_configuration.py`。

描述符声明已注册方法和规划能力，使软件包兼容性不进入图路由代码。本地能力可以执行；显式 `planner_only` 能力具有经过测试的不启动进程语义。

<a id="registry"></a>

## 注册表

`descriptors/registry.yaml` 将包名和规范方法 ID 映射到描述符文件，并定义各阶段可用方法顺序：

```yaml
packages:
  flye: flye.yaml
methods:
  assembly.candidate.flye: assembly/flye_candidate.yaml
by_stage:
  assembly:
    - assembly.candidate.flye
```

`by_stage` 中每个方法都是已实现且可选择的能力。多数使用 `planner.execution_kind: local`；`read_qc.length_filter.skip` 特意使用 `planner_only`，表示真实的无进程选择，而不是可执行命令。未来或不可用工具在适配器或 planner-only 行为、路由、配置和测试完成前不得注册。注册表通过 `app.configuration` 加载，使安装包与源码检出执行均不依赖当前工作目录。

<a id="package-descriptor"></a>

## 软件包描述符

包描述符定义工具级能力：

```yaml
package_name: flye
version_constraint: ">=2.9"
language: cli
install_channel: conda
entry_patterns:
  - "flye --nano-raw {reads} --out-dir {output_dir} --threads {threads}"
help_summary: "针对含噪长读段的长读段从头组装器。"
resource_defaults:
  cpu_cores: 32
  memory_gb: 128
  walltime_hours: 48
```

包描述符可描述安装渠道、入口模式、资源默认值和已知陷阱。机器专属环境与包根路径属于 `config/envs.yaml`，不属于能力记录。

包还可以声明 `conditional_required_tools`。这类工具只有在冻结的运行配置
启用对应能力时才加入 readiness 和 dependency snapshot；例如 Trio
`assembly.candidate.hifiasm` 才要求 `yak`，普通 Hifiasm 或其他非 Trio 路线不
会被强制要求。条件能力和环境快照因此保持同一份工具契约。

<a id="method-descriptor"></a>

## 方法描述符

方法描述符将包能力绑定到一个阶段：

```yaml
method_name: assembly.candidate.flye
stage: assembly
package_ref: flye
parameter_catalog: flye
goal: "构建一个 Flye 草稿组装候选。"
required_inputs:
  - name: long_reads
    type: clean_long_reads_or_fofn
    description: "clean 长读段或已批准的长读段 FOFN。"
expected_outputs:
  - name: draft_assembly
    type: draft_assembly
    description: "所选尝试生成的草稿组装 FASTA。"
review_rules:
  - candidate_bundle_exists
planner:
  required_parameters: [threads]
  parameter_defaults:
    threads: 4
```

方法 ID 在注册表、规划器、运行配置、阶段路由、执行证据和产物中统一。明确的规范方法选择优先；运行时仍验证其阶段、输入、读段类型和参数符合描述符及运行配置。

描述符不授权进程执行。执行需要阶段已授权本地运行，并具有完整冻结尝试。

<a id="validation-boundary"></a>

## 校验边界

注册表加载器当前验证所有引用包存在，且每个 `by_stage` 方法存在并声明相同阶段。已提交文件中，注册表键也与描述符名称一致，每个方法恰好出现在一个阶段列表；后两项是当前文件满足的事实，加载器尚未显式强制。

描述符 YAML 无损且严格拒绝未知内容：注册表、包、方法、规划器、输入/输出及资源字段都必须由 schema 声明。未知键在加载时拒绝，不得静默忽略或当作能力元数据。自由形式方法默认值、规划器参数名和值仅允许放在明确的 `defaults`、`parameter_defaults`、条件 `condition` 映射中。

<a id="executable-coverage-evidence"></a>

## 可执行覆盖证据

`tests/test_registry_executable_invariant.py` 从导入的 `MethodExecutionLifecycle` 实例导出全部本地方法 ID，不维护方法名白名单。每个本地描述符都验证：适配器以精确生命周期构建共享 freeze/run/collect 图；外层阶段 builder 包含方法节点；冻结游标调用同一生命周期的共享 run 边界。测试从生产规划目录导出 planner-only ID，证明两类互斥且覆盖注册表，并跟踪 `read_qc.length_filter.skip` 到运行时 `skip` 的有意转换。该路线保留原始路径，不创建 length-filter 冻结尝试，也不调用 Runner。
