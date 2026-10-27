<a id="autohic-hi-c-scaffolding"></a>

# AutoHiC Hi-C 染色体挂载

> **状态：** 作为唯一 Hi-C scaffolding 方法已实现
>
> **阅读场景：** 追踪染色体级 scaffolding、AutoHiC 参数审批、执行、结果采纳或重试。
>
> **来源：** [运行时编排](runtime-orchestration.md)中的父阶段选择、[数据与存储](data-and-storage.md)中的当前资源、[bootstrap](bootstrap.md)描述的活动主机映射、schema-3 [参数推荐](parameter-recommendation.md)。
>
> **下游：** 已采纳的 `assembly` 资源、下游阶段、持久执行/评审证据、[评审与恢复](review-and-resume.md)。
>
> **依赖契约：** [阶段契约](../reference/stage-contracts.md)、[描述符](../reference/descriptors.md)、[参数推荐](../reference/parameter-recommendation.md)、[方法执行生命周期](../reference/execution-lifecycle.md)、[执行证据](../reference/execution-evidence.md)、[资源交接](../reference/resource-handoff.md)。
>
> **代码依据：** `app/stages/hic_scaffolding/builder.py`、`app/stages/hic_scaffolding/routing.py`、`app/stages/hic_scaffolding/nodes.py`、`app/stages/hic_scaffolding/parameters/workflow.py`、`app/stages/hic_scaffolding/method_application/autohic.py`、`app/execution/autohic_runtime.py`、`app/stages/hic_scaffolding/method_application/support.py`、`tests/test_hic_scaffolding.py`、`scripts/autohic/`、`tests/test_autohic_recovery_tools.py`。

<a id="stage-boundary"></a>

## 阶段边界

仅当冻结的有序计划中包含 `hic_scaffolding` 时，才存在运行时 occurrence；缺少它既不访问节点，也不创建旁路或 skip 事件。该 occurrence 激活后，编译阶段直接运行 AutoHiC。没有单项目方法选择门、阶段内 skip 分支或空操作 AutoHiC 结果。

稳定方法 ID `hic_scaffolding.candidate.autohic` 标识描述符、目录、推荐、尝试、产物和 Ledger 事件，不意味着当前可选择其他 Hi-C 方法。

阶段入口通过资源解析器解析当前采纳的组装，并以资产注册表身份解析配对 Hi-C 成员；同时要求已注册 AutoHiC 描述符和本地执行模式。缺失或无效的阶段要求产生阶段明确的 requirements/failure 结果，不改写成“未选择 Hi-C”。阶段游标为 `blocked_missing_requirements`，但顶层监督会终止运行，不开启可恢复人工评审阻塞。

<a id="implemented-flow"></a>

## 已实现流程

```text
父图选择 hic_scaffolding
  -> 解析已采纳 contig 组装 + 配对 Hi-C 读段
  -> 构建 schema-3 AutoHiC 推荐
  -> 阻塞等待参数审批
  -> 冻结精确 AutoHiC 配置、脚本、依赖和输出
  -> 共享本地运行并持久化执行证据
  -> 收集染色体 FASTA、contact map、assembly 文件和 HTML 报告
  -> 阻塞等待独立采纳评审
  -> 采纳候选、保留旧组装、请求新重试或停止
```

参数审批和染色体组装采纳是两个不同的人工决策。批准稀疏映射只授权一次可执行尝试，不采纳该尝试的生物学输出；最终门禁读取已发布证据，决定下游应使用哪个组装。

<a id="adapter-and-execution-boundary"></a>

## 适配器与执行边界

AutoHiC 适配器只加载已验证的已批准 schema-3 参数链。它解析执行器值、规范已提供事实和已批准模型覆盖，不合并前一次重试映射，然后为共享生命周期物化冻结参数证据、AutoHiC 配置、保留脚本、命令记录、精确输出路径和类型化依赖。

机器级 AutoHiC、Juicer、3D-DNA、Python 和模型位置来自活动主机映射，可人工维护或通过 [bootstrap](bootstrap.md)维护，并在运行中冻结。组装、Hi-C 读对和提供的限制性内切酶是运行级事实。边界无法形成完整可执行尝试时，适配器拒绝冻结。

执行使用[方法执行](execution.md)描述的生产本地子进程 Runner。生成的 wrapper 调用配置的 AutoHiC 入口；Gene Agent 不在图中重写 Juicer、3D-DNA、错误检测或染色体分配。不存在 AutoHiC 远程/调度器 Runner。

就绪与产物校验是独立边界。冻结验证声明的可执行文件、模型、输入材料和输出路径，但不要求进程运行前输出已存在。进程报告成功后，收集会在首次发布前校验每个冻结 `expected_outputs` 成员。AutoHiC 要求染色体 FASTA、`.hic`、`.assembly` 和 HTML 报告。每项必须是文件，除非不可变索引已有可在崩溃回放中恢复的同一尝试本地产物。缺失或非文件且无该记录时，产生终态 `failure_kind: collect`，不打开评审候选或采纳门，也不发布四项评审产品、评审包或 QC 报告。

正常 wrapper 成功后，收集发布染色体级 FASTA、`.hic`、`.assembly` 和 HTML 评审产物，以及紧凑评审/QC 包和规范执行引用。共享 Runner 已拥有 stdout、stderr 与进程证据；收集不重复登记日志。发布有意不具事务性：若后续发布在早先产品移动并索引后失败，早先产品保留为已索引孤儿；收集不记录 `attempt_succeeded` 或 `artifact_registered` 语义事件，不产生评审候选或门，也不重试或回滚。

<a id="gpu-and-cpu"></a>

## GPU 优先与 CPU 兼容

`resource_defaults.gpu_required: false` 和方法 `resource_hints.gpu_required: false` 表示 GPU 不是必需条件，不会强制使用 CPU。正式 wrapper 将 `app/execution/autohic_runtime.py` 源码嵌入冻结脚本，在方法映射选定的 AutoHiC Python 环境中运行，再启动官方 `autohic.py`。策略执行实际 CUDA/MMCV NMS 探测后优先 GPU；CPU 版 PyTorch 会走 CPU，即使主机有 GPU。

有 GPU 的主机应在专用 AutoHiC 环境中安装相互兼容的 CUDA 版 PyTorch、torchvision、MMCV 算子，并在活动 `config/envs.yaml` 的 `methods.hic_scaffolding.candidate.autohic.conda_env` 或 `conda_prefix` 指向该环境。未配置 GPU 的主机可以继续映射原 CPU 环境；无需把 GPU 改成必需资源，也无需修改参数推荐 schema。

验证应包含实际模型推理：先检查 PyTorch CUDA 状态，再运行 CUDA MMCV 算子和错误/染色体两个模型各一张图。`nvidia-smi` 能看到设备不能证明 AutoHiC 环境支持 GPU。CUDA 版环境还应在 `CUDA_VISIBLE_DEVICES=''` 下测试 CPU 推理。

适配器在启动官方入口前，仅在当前进程中替换 `src.common.error_pd` 和 `src.utils.get_chr_data` 的模型初始化、推理函数引用，覆盖错误检测与染色体识别，不写入已安装 AutoHiC 源码。识别到 CUDA 初始化/推理故障时，在异常处理块退出后释放残留；推理失败将模型移到 CPU、重试当前图片，后续模型调用保持 CPU。普通输入/程序错误原样失败。设备事件和进度位于尝试工作目录，并打印到正式执行日志。源码随冻结脚本固定；已冻结尝试继续执行其原脚本，不因应用升级自动获得新策略。真实 GPU 和完整正式运行仍需目标主机验收。

<a id="resource-failure-and-external-recovery"></a>

## 资源失败与独立恢复工具

应区分 RAM、GPU 显存、Linux 文件系统空闲和 Windows VHDX 文件占用。Juicer 的 SAM、异常/未比对分类、排序文本以及 `splits/`、`aligned/` 中的重复中间文件可以使磁盘耗尽；`No space left on device` 不能诊断为 AutoHiC RAM 泄漏。删除 Linux 文件会释放内部空间，动态 VHDX 的宿主文件通常不会同时缩小。磁盘压缩应安排在运行停止、WSL 关闭后；不能在活动任务中关机压缩。

上游 `src/common/run.sh` 和 `autohic.py` 的嵌套子进程存在未向上传播失败的路径：错误后仍可能打印阶段 finished。外层 wrapper 的 `set -e` 无法恢复被上游吞掉的退出码。恢复不能只看日志末尾或重新调用完整 `autohic.py`；后者会重新进入 Juicer/3D-DNA，重复计算并可能覆盖同名产物。正式 Runner 的完整输出校验仍必须保留。

`scripts/autohic/` 是 Linux/Python 3.9 的独立运维工具，使用 AutoHiC 自身环境，从仓库根以 `python -m scripts.autohic.…` 调用，不依赖编排器的 PyTorch，也不修改已安装 AutoHiC 源码：

- `device.py`：兼容导出应用包 `app/execution/autohic_runtime.py` 的共享设备策略。`auto` 探测实际 CUDA/MMCV NMS 后优先 GPU；启动不可用或识别到的 CUDA 初始化/推理错误时退回 CPU。推理失败只重试当前图片，之后保持 CPU。`cpu` 强制 CPU；`cuda` 要求 GPU，失败直接退出。只在退出异常处理块后释放初始化残留，记录设备事件和已处理图片数。设备上下文严重损坏导致模型无法移回 CPU 时仍会失败，不保证恢复所有 CUDA 错误。
- `check_environment.py`：实际运行 CPU NMS、所选设备的探针及错误/染色体两个模型各一张图片，输出版本和实际设备。必须在无 GPU 可见的条件下单独验证 CPU 路径。
- `recover.py`：复用操作者确认来源的 `.hic/.assembly` 配对或图片集，执行与官方 `onehic.py` 对应的单轮错误检测/调整，可选调用官方 3D-DNA post-review。检查磁盘预留、配对来源记录、推理范围、assembly 长度/placement、图片文件、子进程退出码和输出；图片数量或坐标覆盖与官方窗口不匹配时仅提示，继续处理索引中的图片。使用目录锁、全新输出目录和持久状态防止重复写入。磁盘预留是操作者设置的运行余量，不是完整峰值空间预测，也不约束 RAM。

`.hic` 可读取且 assembly 总长一致并不能单独证明二者配对。必须核对同一生产轮次的 cprops/asm、原始序列名/长度等来源，再用 `--pair-provenance` 记录依据。官方 `get_hic_real_len` 使用第一个 placement group，并按 map/assembly 比例换算坐标；多组 rawchrom 输入可能只覆盖很小一段。`--expected-inference-span-bp` 要求操作者明确确认官方推理坐标范围，不能仅凭文件名选输入。已有图片还必须确认来自该 contact map；数量校验不构成来源证明。

AutoHiC 上游 `src/utils/get_chr_data.py::divide_chr` 将 inclusive 染色体末端加一作为切点，但对片段末端/已有连接处仍调用切割，会生成零长度 fragment；末尾还用跨两碱基查询选择下一组起点。`scripts/autohic/patches/chromosome-boundaries.patch` 是针对上游 `4dc5ad9eba62bba569d7934261df2b3ea630faa2` 的独立补丁：保留官方 cut/re-cut 操作，只在片段内部切割，再按累计末端分组；支持反向 placement、重复切割和 map/assembly 比例换算，保留推理范围外的未分配片段。它不改变模型、阈值或预测边界，也不靠删除零长度结果掩盖错误。补丁需要在目标 AutoHiC 检出显式应用；Gene Agent 不会自动修改外部软件。安装和显式回归见[部署指南](../../deployment/migration.md#autohic-chromosome-boundary-patch)。

独立恢复不执行完整 AutoHiC 的候选轮次选择、迭代纠错、染色体分配或 HTML 报告生成；其默认关闭 debris 调整，正式 `onehic.py` 默认值不同，操作者必须核对参数。它不导入 Gene Agent attempt、不改 Ledger/checkpoint，也不能替代正式四项产物的成功/采纳。正式 wrapper 与独立工具共用设备策略；清理、子阶段恢复和错误传播的待办见[一致性缺口](../reference/conformance.md#autohic-resource-recovery-gap)。操作命令见[运行与恢复](../../user_guide/run-and-recovery.md#autohic-external-recovery)，GPU 环境迁移见[新主机配置](../../deployment/migration.md#autohic-gpu-environment)。

<a id="adoption-and-retries"></a>

## 采纳与重试

接受候选会验证已发布 FASTA，并将 `resource_refs["assembly"]` 替换为带来源的 Hi-C 组装。保留旧组装会保留其字节与候选关系，但在当前 Hi-C occurrence 下重新发布中央资源。请求参数重试会封存当前尝试，带不可变执行和评审证据返回推荐。

执行失败重试由明确操作者策略和有界预算控制；评审触发的重试有自己的预算。两者都创建新的 schema-3 推荐和尝试专属工作区，不修改旧映射或覆盖其产物。

<a id="current-limits"></a>

## 当前限制

- AutoHiC 是唯一注册的 Hi-C 方法，其他 scaffolder 没有实现选择路径。
- QC 包明确记录未应用自动 QC 阈值。最终生物学采纳是人工决策，尚未自动采纳。
- 生产执行在 CLI 主机本地；调度器提交、进程重新附着和远程编排尚未实现。
- 需求失败、参数拒绝、重试耗尽和执行失败是不同结果，没有任何隐藏 skip 分支。
