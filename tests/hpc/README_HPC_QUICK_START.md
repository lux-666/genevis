# HPC 快速启动指南 / HPC Quick Start Guide

> 当前 smoke 合同以本目录脚本和 `hpc_env.sh.example` 为准。

## 快速开始 / Quick Start

### 1. 首次配置 / First Time Setup

在 HPC 上配置好 `work/genevis/tests/hpc/hpc_env.sh` 后，可以这样运行：

```bash
cd /path/to/gene_agent/tests/hpc

# 方式1：使用快速启动脚本（推荐）
./quick_run.sh --setup    # 交互式配置向导
./quick_run.sh --check    # 检查配置
./quick_run.sh            # 运行完整流程

# 方式2：直接运行测试脚本
./test_09_e2e_pipeline.sh

# 全套分阶段检查（00→09；assembly/Hi-C 会实际运行）
./run_all.sh
```

### 2. 配置文件 / Configuration

配置文件位置：`tests/hpc/hpc_env.sh`

**必需配置项 / Required:**
```bash
LONG_READS="/path/to/long_reads.fq.gz"           # ONT/PacBio 长读长数据
PLATFORM="ont"                                   # ont | clr | pacbio_hifi
BUSCO_LINEAGE_PATH="/db/busco/actinopterygii_odb10"
BUSCO_LINEAGE_NAME="actinopterygii_odb10"
HPC_PARAMETER_APPROVAL_MODE="manual"           # manual | approve
```

`GENOME_SIZE_ESTIMATE` 是可选正整数。只要有短读或长读，`genome_survey` 都会运行；
填写值只覆盖 survey 的 genome-size 结果，杂合率、重复比例和原始测量值仍会保留。

`manual` 会在 hifiasm/hifiasm_ont 参数推荐后停在
`parameter_approval`；后续 stage 也可能进入各自的 human gate。`run_all.sh`
会把“等待人工输入”记为 `SKIP (77)`，继续执行仍可独立运行的检查，并在该
step 的日志中保留 session ID 与恢复命令。这不是测试失败。

使用下面的 TUI 命令接回同一个 durable session：

```bash
python3 -m app.entrypoints.run tui \
  --resume-session <session-id> \
  --data-dir ./data_hpc_test
```

也可以直接运行 `gene-agent review --session-id ...`，或执行脚本打印的
`inspect --write-decision-template` / `resume --decision-file` 命令。
只有明确的无人值守 smoke test 才应设置 `approve`；该路径仍持久化并校验
attempt/task/recommendation hash 绑定。`approve` 只处理 assembly 参数推荐，
不会自动替人接受 assembly review、Hi-C 或 T2T 的生物学决策。

每次 `run_all.sh` 的日志都有独立目录，避免覆盖前一次实机结果：

```text
${TEST_DATA_ROOT}/test_logs/run-all-YYYYMMDD-HHMMSS/
```

**可选配置（启用 Hi-C 和 T2T）/ Optional (Enable Hi-C & T2T):**
```bash
HIC_R1="/path/to/hic_R1.fq.gz"                  # Hi-C 双端测序 R1
HIC_R2="/path/to/hic_R2.fq.gz"                  # Hi-C 双端测序 R2
HIC_RESTRICTION_ENZYME="DpnII"                   # 限制性内切酶
HIC_AUTOHIC_ERROR_MODEL="/path/to/error_model.pth"
HIC_AUTOHIC_CHROMOSOME_MODEL="/path/to/chromosome_model.pth"
```

## 流程说明 / Pipeline Overview

### 完整流程 / Full Pipeline

当配置了 Hi-C 数据时，流程将依次执行：

1. **read_qc** - 读长质控和过滤
2. **genome_survey** - 基因组调查（k-mer 分析）
3. **assembly** - 基因组组装（hifiasm/hifiasm_ont 为当前主目标；Flye/NextDenovo 为轻量 smoke）
4. **assembly_review** - 组装质量评估（QUAST + BUSCO）
5. **hic_scaffolding** - AutoHiC 染色体挂载与纠错 ⭐ 实际执行生物学软件
6. **t2t** - 端粒修补（T2T telomere finishing）⭐ 实际执行生物学软件

### 部分流程 / Partial Pipeline

如果没有配置 Hi-C 数据，流程将在 `assembly_review` 后停止：

1. read_qc → genome_survey → assembly → assembly_review ✓

## 实际执行的生物学软件 / Biological Software Executed

### Hi-C 阶段 / Hi-C Stage
- **AutoHiC**: 阶段唯一入口；内部编排 Juicer、3D-DNA、深度学习错误检测、组装修正和染色体划分
- **Juicer**: 由 AutoHiC 调用，完成 Hi-C 比对和接触数据生成
- **3D-DNA**: 由 AutoHiC 调用，完成初始挂载与修正后重跑

输出文件 / Outputs:
- `*scaffolded*.fasta` - 挂载后的组装序列
- `*.hic` - Hi-C 接触矩阵
- `*.assembly` - 3D-DNA 组装描述文件
- `result.html` - AutoHiC 前后对比报告

### T2T 阶段 / T2T Stage

`test_07_t2t.sh` 优先使用显式配置的 `T2T_ASSEMBLY`/`T2T_ONT_READS`。在
顺序执行 `run_all.sh` 时，它也会复用 Test 06 的最新 Hi-C scaffold（否则复用
最新 assembly），并在 ONT/CycloneSeq 且只有一个长读文件时复用该 reads。
仍无法组成完整输入时返回明确的 SKIP；只配置一项或真实运行缺产物时返回 FAIL。

- **Telomere detection**: 端粒序列检测
- **Gap analysis**: 缺口分析
- **Read remapping**: 长读长重新比对
- **Consensus patching**: 基于共识序列的缺口填补

输出文件 / Outputs:
- `*patched*.fasta` - 修补后的组装序列
- `*report*.json` - T2T 分析报告

## 常见问题 / FAQ

### Q1: 如何只运行到某个阶段？
**A:** 正常用户流程直接运行 `gene-agent tui`，在对话中描述目标；不需要编辑
`local_exec` 或 run-config。`tests/hpc` 的配置文件只是可重复集成测试夹具。若只想
从某个测试 step 开始，可运行 `./run_all.sh 5`，或直接执行对应的 `test_*.sh`。

### Q2: Hi-C 执行失败怎么办？
**A:** 检查以下配置：
- `HIC_RESTRICTION_ENZYME`: 必须与 Hi-C 建库酶一致
- `HIC_AUTOHIC_ERROR_MODEL` 与 `HIC_AUTOHIC_CHROMOSOME_MODEL`: 两个预训练模型都必须存在
- `config/envs.yaml`: AutoHiC、Juicer、3D-DNA 的安装根目录和 AutoHiC 专用 conda 环境必须可用

### Q3: 如何查看会话状态？
**A:** 使用 inspect 命令：
```bash
python3 -m app.entrypoints.run inspect \
    --session-id <your-session-id> \
    --data-dir ./data_hpc_test \
    --trace
```

### Q4: 流程在哪里会阻塞等待人工干预？
**A:** 阻塞点包括：
- Parameter approval（每个 initial/retry attempt 都必须独立批准）
- Assembly route selection（如果自动选择置信度低）
- Assembly review（除非设置 `review_policy: {mode: "auto"}`）
- Hi-C scaffolding（如果 execution_mode != local）
- T2T finishing/review（当 stage 提出需要人工确认的变更）

`test_09` 的 review policy 为 auto，但不会绕过独立的
`parameter_approval`。除非 HPC 配置显式选择测试专用的
`HPC_PARAMETER_APPROVAL_MODE=approve`，参数推荐仍需要人工批准。

human gate 并不限定为“两阶段”。同一个 session 可以按实际执行顺序多次经历：
上游 profile 产生后的 assembly parameter approval、assembly review，以及后续
Hi-C/T2T 的 stage-specific gate。每次恢复都继续原 session，已完成 stage 不应重跑。

## 工具环境 / Tool Environments

所有生物学软件的环境配置在 `config/envs.yaml` 或 `config/envs.local.yaml`。

**推荐在 HPC 上创建 `config/envs.local.yaml`：**
```yaml
methods:
  hic_scaffolding.candidate.autohic:
    conda_env: autohic
    required_tools: [python3.9, bwa, java, quast.py]
    package_tools: [autohic, juicer, three_d_dna]
  
  t2t.detect_telomeres:
    conda_env: t2t_toolkit

stages:
  hic_scaffolding:
    conda_env: autohic
  
  t2t:
    conda_env: t2t_toolkit
```

## 输出文件结构 / Output Structure

```
data_hpc_test/runs/hpc-test09-e2e-YYYYMMDD-HHMMSS/
├── artifacts/
│   ├── read_qc/          # 质控后的读长
│   ├── genome_survey/    # 基因组调查结果
│   ├── assembly/         # 组装结果
│   ├── assembly_review/  # 质量评估报告
│   ├── hic_scaffolding/  # Hi-C 挂载结果 ⭐
│   └── t2t/              # T2T 修补结果 ⭐
├── work/                 # 中间文件和执行脚本
├── ledger/
│   └── events.jsonl      # 执行日志
├── asset_registry.json   # 资源注册表
└── checkpoints/          # 检查点文件
```

## 参考其他测试脚本 / Reference Other Tests

- `test_06_hic.sh` - 单独的 Hi-C 挂载测试
- `test_07_t2t.sh` - 单独的 T2T 测试
- `test_04_assembly_dedup.sh` - 组装、参数批准与去冗余测试
- `common.sh` - 共享的测试工具函数

## 联系 / Contact

遇到问题请查看：
- 主项目 `CLAUDE.md` - 编码原则
- 当前架构规范 `docs/architecture/`
- 执行日志 `ledger/events.jsonl`
