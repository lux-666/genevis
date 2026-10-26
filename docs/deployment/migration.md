<a id="new-host-setup"></a>

# 新主机配置

本指南配置全新的 Gene Agent 检出，仅拥有安装和主机就绪。启动、检查和恢复会话见[运行与恢复](../user_guide/run-and-recovery.md)，持续 Mac/服务器 Git 流程见[Mac 开发与服务器执行](mac_dev_server_pull.md)。部署入口及不同职责见[部署入口](README.md)。

<a id="prerequisites"></a>

## 前置条件

- `PATH` 中有 Conda 或 Mamba。
- Python 3.11 或更新版本。
- 足够所选方法使用的 CPU、内存、存储和运行时间。
- 服务器本地生物信息学数据库、BUSCO lineage、软件包和模型。

GPU 不是所有 contig 运行的统一要求，只有所选方法或外部软件包明确需要时才要求。

<a id="what-git-carries"></a>

## Git 携带的内容

Git 携带应用代码、`pyproject.toml`、描述符、测试、现行文档、符合 schema 的示例和辅助脚本。

以下保留为机器本地：

- `.env` 和凭据。
- 活动 `config/envs.yaml`（已提交种子为 `docs/examples/envs.yaml.example`）。
- Conda 环境和外部软件包安装。
- BUSCO lineage、数据库和模型权重。
- 真实 FASTQ/FASTA/BAM 输入及私有运行配置。
- 运行数据、checkpoint、Ledger、报告和产物。

<a id="install-the-orchestrator"></a>

## 安装编排器

```bash
conda create -n gene_agent python=3.11 -y
conda activate gene_agent
python -m pip install -c constraints/ci.txt -e '.[dev]'
```

`'.[dev]'` 的引号防止 shell 通配符展开。Python 运行与开发依赖仅声明于 [`pyproject.toml`](../../pyproject.toml)。`gene_agent` 是文档约定的编排器环境名，不从 `config/envs.yaml` 读取。以下命令假设它保持激活。

<a id="verify-the-code-checkout"></a>

## 验证代码检出

安装生物工具或编辑主机路径前运行纯代码门禁：

```bash
bash /path/to/gene_agent/scripts/server_test.sh
```

它使用当前激活的 Python，不选择其他 Conda 环境、不调用 Node/npm、不下载包。

<a id="provision-the-contig-tool-set"></a>

## 配置 contig 工具集

仓库中的五阶段 contig 示例有意不绑定组装器。以下共享环境覆盖 contig 路线和声明的 Assembly 候选；通用主机种子另含 GapCloser、Base Polish、AutoHiC，这些能力仍需分别安装其额外依赖：

```bash
# 注意包名：conda-forge::jellyfish 是无关的 Python 字符串匹配库，
# 不提供所需可执行文件。
conda create -n env_geno_exec \
  --channel conda-forge \
  --channel bioconda \
  --strict-channel-priority \
  seqkit \
  kmer-jellyfish \
  genomescope2 \
  flye \
  nextdenovo \
  hifiasm \
  minimap2 \
  purge_dups \
  gzip \
  quast \
  busco \
  -y
```

该命令是安装便利，不是需求权威。`scripts/server_check.sh` 从 `docs/examples/contig_run_config.template.json` 导出固定阶段方法闭包，经活动映射解析并检查每项命令/软件包需求。尤其 `purge_dups` 必须暴露全部辅助命令，安装包本身不证明就绪。

Assembly 在上游证据具备后选择方法。应声明并安装该主机上运行可能选择的每个候选；bootstrap 检查声明目录。所选候选在执行前从冻结依赖快照再做阶段预检。GapCloser 另需 `tgsgapcloser` 与 GNU `getopt`，Base Polish 另需 `racon`；以活动映射声明为准。AutoHiC、Juicer、3D-DNA、模型和生物数据库是外部资源，不包含在上述 Conda 安装命令中。

<a id="autohic-gpu-environment"></a>

## AutoHiC GPU 环境

AutoHiC 的 Python/深度学习栈与编排器分开安装。保留已验证 CPU 环境，克隆专用 GPU 环境；仅切换方法主机映射。以下是已验证的 Linux x86-64/Python 3.9、GTX 1080 Ti 兼容组合，供迁移旧版 AutoHiC 使用；其他显卡/架构需重新选择配套版本并执行实际模型验证，不应直接升级整个环境到最新 PyTorch/MMDetection。

```bash
conda create -n autohic_gpu --clone autohic -y
conda install -n autohic_gpu -c pytorch -c defaults \
  pytorch=1.10.1 torchvision=0.11.2 cudatoolkit=11.3.1 \
  pytorch-mutex=1.0=cuda -y
conda run -n autohic_gpu python -m pip install --no-deps --force-reinstall \
  https://download.openmmlab.com/mmcv/dist/cu113/torch1.10.0/mmcv_full-1.3.17-cp39-cp39-manylinux1_x86_64.whl
```

这组环境还包含 MMDetection 2.11.0、NumPy 1.24.3、timm 0.6.13，以及原 AutoHiC 自带的模型代码/权重。这里的安装命令是迁移配方，并非已在所有新主机验证的锁文件；先检查 solver 计划，避免混入 CPU PyTorch 或不匹配的 torchvision/MMCV。主机还需兼容 CUDA 11.3 的 NVIDIA 驱动，WSL 由 Windows 驱动提供；`nvidia-smi` 的 CUDA 上限不等于 Python 环境实际编译版本。

从仓库根，用一张已有的 AutoHiC 接触图图片实际检查两个模型；输出目录必须不存在：

```bash
conda run --no-capture-output -n autohic_gpu python \
  -m scripts.autohic.check_environment \
  --autohic-root /opt/AutoHiC --image /data/hic/png/5000/example.jpg \
  --output /data/checks/autohic-gpu --device cuda
CUDA_VISIBLE_DEVICES='' conda run --no-capture-output -n autohic_gpu python \
  -m scripts.autohic.check_environment \
  --autohic-root /opt/AutoHiC --image /data/hic/png/5000/example.jpg \
  --output /data/checks/autohic-cpu --device auto
```

检查 `environment.json` 中两个模型的实际 device 和版本，第二条必须为 CPU。`--device cuda` 失败退出，不会隐式退回；`auto` 的精确边界见[AutoHiC 设备策略](../architecture/topics/autohic.md#resource-failure-and-external-recovery)。这些工具必须从源码检出运行，当前应用 wheel 只包含 `app`。

如果旧 PyTorch 在新系统上导入时报精确错误 `libtorch_cpu.so: cannot enable executable stack as shared object requires: Invalid argument`，先确认该共享库的 ELF GNU_STACK 标志。仅对此错误，可在保留备份后，用支持 `--clear-execstack` 的 patchelf 修复新 GPU 环境中的私有副本。Conda 文件可能与包缓存硬链接，不能原地修改共享 inode：

```bash
conda activate autohic_gpu
# torch 尚无法导入，用 Python 的安装路径定位；确认路径属于新 GPU 环境。
autohic_lib=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"] + "/torch/lib/libtorch_cpu.so")')
cp --preserve=all -- "$autohic_lib" "$autohic_lib.before-execstack-fix"
cp --preserve=all -- "$autohic_lib" "$autohic_lib.private"
patchelf --clear-execstack "$autohic_lib.private"
mv -- "$autohic_lib.private" "$autohic_lib"
```

仅在确认该兼容性问题时执行，不批量修改其他库，也不修改 CPU 环境或 Conda 包缓存。然后重跑上述 GPU 与 CPU 模型检查。活动 `config/envs.yaml` 中 `methods.hic_scaffolding.candidate.autohic.conda_env` 改为 `autohic_gpu`，其余包根和 Python 路径保持目标主机实际配置；若声明 `conda_prefix`，须同步指向同一个环境。不要修改已冻结会话的环境快照。`gpu_required: false` 保持不变，表示无 GPU 的机器仍可用 CPU。

<a id="autohic-chromosome-boundary-patch"></a>

### AutoHiC 染色体边界补丁

上游 `4dc5ad9eba62bba569d7934261df2b3ea630faa2` 的 `divide_chr` 存在末端切割产生零长度 fragment 的缺陷，与 GPU/CPU 设备无关。仓库附带保持模型和预测边界不变的最小作用域补丁，原因见[AutoHiC 专题](../architecture/topics/autohic.md#resource-failure-and-external-recovery)。在目标 AutoHiC 源码根应用，先检查已有修改及补丁上下文：

```bash
genevis_checkout=/path/to/genevis
autohic_checkout=/opt/AutoHiC
boundary_patch="$genevis_checkout/scripts/autohic/patches/chromosome-boundaries.patch"
git -C "$autohic_checkout" status --short
git -C "$autohic_checkout" apply --check "$boundary_patch"
git -C "$autohic_checkout" apply "$boundary_patch"
```

检查不通过时不要强制覆盖目标主机修改；已应用时可用 `git apply --reverse --check` 核对。该补丁只修改 `src/utils/get_chr_data.py::divide_chr`，正式 `autohic.py` 和外部恢复的 `split_chr` 调用都使用此函数。不要在仍运行该子阶段时变更源码。

用编排器的 pytest 驱动 AutoHiC 自身 Python 验证 7 组真实函数案例，包含正向/反向末端、已有连接、内部切割、重复反向切割和比例坐标。普通回归不会探测或修改外部软件：

```bash
cd "$genevis_checkout"
AUTOHIC_TEST_ROOT="$autohic_checkout" \
AUTOHIC_TEST_PYTHON=/path/to/conda/envs/autohic_gpu/bin/python3.9 \
  python -m pytest -q -m real_tool tests/test_autohic_boundary_patch.py
```

这条检查不运行模型或完整 Juicer/3D-DNA，但会导入 AutoHiC 的依赖；两项环境变量必须显式提供。真实样本恢复还需校验全部 fragment 为正长且恰好放置一次、总长不变、每组末端与模型边界一致，再执行 post-review。

<a id="conditional-services-and-data"></a>

## 条件性服务与数据

为使用 LLM 的阶段创建私有 `.env`，例如：

```dotenv
GENE_AGENT_LLM_PROVIDER=openai_compatible
GENE_AGENT_LLM_BASE_URL=...
GENE_AGENT_LLM_API_KEY=...
GENE_AGENT_LLM_MODEL=...
GENE_AGENT_LLM_TEMPERATURE=0
GENE_AGENT_LLM_MAX_TOKENS=16000
GENE_AGENT_LLM_TIMEOUT_SECONDS=180
```

不要提交它。顾问模式的 Assembly 方法和参数推荐需要可达 LLM；独立确定性基线模式的就绪要求见[推荐策略](../architecture/reference/recommendation-policy.md)。Assembly Review 还要求 `user_metadata.target_species`、NCBI 快照根、兼容已安装 BUSCO lineage；运行选择见[Assembly Review 阶段契约](../architecture/reference/stage-contracts.md#6-assembly-review)。AutoHiC 额外需要声明的外部包根、Juicer/3D-DNA/JAR 布局和模型。

通过唯一[快照权威文档](../../data/reference/ncbi/README.md)安装 NCBI 快照。只有主机暴露该能力时才安装 AutoHiC/Juicer/3D-DNA/JAR/模型。跳过的可选能力以后可加入主机映射。

<a id="configure-the-active-host-mapping"></a>

## 配置活动主机映射

设置 `GENE_AGENT_ENVS_CONFIG` 时执行读取该文件，否则读取 `config/envs.yaml`。命令可能从不同目录启动时使用绝对覆盖路径。`docs/examples/envs.yaml.example` 只是通用 bootstrap 种子，绝不是第二运行输入。

活动映射可手写或使用可选助手：

```bash
gene-agent bootstrap
```

新主机在数据库或 AutoHiC 安装前也可创建核心 Conda 映射，对可选组回答否即可。安装跳过的资源后，可手动添加完整 YAML 组或重跑普通 `gene-agent bootstrap` 选择配置。`bootstrap --update` 替换已有管理值，不是添加缺失可选组的必需入口。每次辅助变更先打印精确计划，等待空回车确认后写入。

手动编辑同样有效。手工复制通用种子时，替换全部保留占位符，删除尚未配置的可选组，不保留半填数据库或包组。跳过 AutoHiC 时一并删除四个包项和 `methods.hic_scaffolding.candidate.autohic`。

数据库安装完成后填写唯一 `databases:` 块，注册已安装 BUSCO lineage 别名和 NCBI 快照根。每运行 RunConfig 不重复数据库路径、版本、来源或 lineage 选择。Assembly Review 的 NCBI 拉丁学名由 manifest `user_metadata.target_species` 提供；系统从根下选择的冻结 NCBI 快照和主机安装注册表推导 lineage。RunConfig 的 BUSCO 设置只接受执行模式和线程数。

NCBI 安装器从活动映射解析根目标，写入一个有名子目录。根尚不存在时，先人工添加预定根路径，再运行安装器，最后用 bootstrap 或 `--verify-only` 验证安装。

Bootstrap 不安装任意包或 Conda 环境。它验证包探针、实施有界且已声明的 Juicer 布局修复、探测活动映射的每个方法项。精确边界由[项目 Bootstrap](../architecture/topics/bootstrap.md)维护。Bootstrap 目录验证不能替代下方 contig 专属数据库和工具检查。

<a id="verify-host-readiness"></a>

## 验证主机就绪

就绪脚本从自身路径定位仓库根，可从任意当前目录调用：

```bash
bash /path/to/gene_agent/scripts/server_check.sh
```

范围如下：

- `server_check.sh [run_config.json]`：编排器前置条件与给定配置的固定工具/包/数据库闭包，默认 contig 模板；延迟 Assembly 候选就绪由阶段拥有。
- `server_test.sh`：格式、lint、类型、普通回归、编译和 diff 门禁。
- 真实数据冒烟：操作者使用服务器本地输入显式运行，见[运行与恢复](../user_guide/run-and-recovery.md)。

只有命令以零退出并打印预期完成证据时，才能称门禁成功。
