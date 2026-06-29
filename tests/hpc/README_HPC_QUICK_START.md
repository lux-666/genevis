# HPC 快速启动指南 / HPC Quick Start Guide

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
```

### 2. 配置文件 / Configuration

配置文件位置：`tests/hpc/hpc_env.sh`

**必需配置项 / Required:**
```bash
LONG_READS="/path/to/long_reads.fq.gz"           # ONT/PacBio 长读长数据
GENOME_SIZE_ESTIMATE=500000000                   # 基因组大小估计（bp）
PLATFORM="ont"                                   # ont | clr | pacbio_hifi
BUSCO_LINEAGE_PATH="/db/busco/actinopterygii_odb10"
BUSCO_LINEAGE_NAME="actinopterygii_odb10"
```

**可选配置（启用 Hi-C 和 T2T）/ Optional (Enable Hi-C & T2T):**
```bash
HIC_R1="/path/to/hic_R1.fq.gz"                  # Hi-C 双端测序 R1
HIC_R2="/path/to/hic_R2.fq.gz"                  # Hi-C 双端测序 R2
HIC_RESTRICTION_ENZYME="DpnII"                   # 限制性内切酶
HIC_CHROMOSOME_COUNT=25                          # 染色体数量
```

## 流程说明 / Pipeline Overview

### 完整流程 / Full Pipeline

当配置了 Hi-C 数据时，流程将依次执行：

1. **read_qc** - 读长质控和过滤
2. **genome_survey** - 基因组调查（k-mer 分析）
3. **assembly** - 基因组组装（Flye/NextDenovo）
4. **assembly_review** - 组装质量评估（QUAST + BUSCO）
5. **hic_scaffolding** - Hi-C 挂载（Juicer + 3D-DNA）⭐ 实际执行生物学软件
6. **t2t** - 端粒修补（T2T telomere finishing）⭐ 实际执行生物学软件

### 部分流程 / Partial Pipeline

如果没有配置 Hi-C 数据，流程将在 `assembly_review` 后停止：

1. read_qc → genome_survey → assembly → assembly_review ✓

## 实际执行的生物学软件 / Biological Software Executed

### Hi-C 阶段 / Hi-C Stage
- **Juicer**: Hi-C 比对和接触矩阵生成
- **3D-DNA**: 基于 Hi-C 的染色体挂载
- **JuiceBox**: 可视化和人工审核（生成 .hic 文件）

输出文件 / Outputs:
- `*scaffolded*.fasta` - 挂载后的组装序列
- `*.hic` - Hi-C 接触矩阵
- `*.assembly` - 3D-DNA 组装描述文件

### T2T 阶段 / T2T Stage
- **Telomere detection**: 端粒序列检测
- **Gap analysis**: 缺口分析
- **Read remapping**: 长读长重新比对
- **Consensus patching**: 基于共识序列的缺口填补

输出文件 / Outputs:
- `*patched*.fasta` - 修补后的组装序列
- `*report*.json` - T2T 分析报告

## 常见问题 / FAQ

### Q1: 如何只运行到某个阶段？
**A:** 修改 `hpc_env.sh` 中的 `local_exec` 列表，或编辑生成的 run-config JSON 文件。

### Q2: Hi-C 执行失败怎么办？
**A:** 检查以下配置：
- `HIC_JUICER_SCRIPTS_DIR`: Juicer 安装路径（包含 common/chimeric_blacklist.awk）
- Java 是否在 conda 环境中可用
- `HIC_CHROM_SIZES`: 如果未设置，脚本会自动从组装中生成

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
- Assembly route selection（如果自动选择置信度低）
- Assembly review（除非设置 `review_policy: {mode: "auto"}`）
- Hi-C scaffolding（如果 execution_mode != local）

当前 test_09 配置了自动模式 (`mode: auto`)，因此不会阻塞。

## 工具环境 / Tool Environments

所有生物学软件的环境配置在 `config/envs.yaml` 或 `config/envs.local.yaml`。

**推荐在 HPC 上创建 `config/envs.local.yaml`：**
```yaml
methods:
  hic_scaffolding.candidate.juicer_3ddna_juicebox:
    conda_env: juicer_3ddna
  
  t2t.detect_telomeres:
    conda_env: t2t_toolkit

stages:
  hic_scaffolding:
    conda_env: juicer_3ddna
  
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
- `test_04_assembly.sh` - 组装测试
- `common.sh` - 共享的测试工具函数

## 联系 / Contact

遇到问题请查看：
- 主项目 `CLAUDE.md` - 编码原则
- 各阶段的任务文档 `docs/tasks/phase_*_*.md`
- 执行日志 `ledger/events.jsonl`
