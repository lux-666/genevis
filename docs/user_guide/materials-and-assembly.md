# 从材料选择组装路线

系统先登记用户实际拥有的材料，执行所选 QC/Genome Survey，再推荐并确认一条组装路线。组装方法不写入 RunConfig；Assembly 根据材料和上游证据选择方法，随后审批参数并执行。

| 材料 | 当前执行方式 |
|---|---|
| HiFi | Hifiasm 是可推荐路线，最终方法在 Assembly 确认 |
| HiFi＋明确的超长 ONT | Hifiasm `--ul` 联合输入；普通 ONT 不自动认定为超长读段 |
| ONT-only | Hifiasm `--ont`、Flye 或 NextDenovo 中推荐一条兼容路线 |
| HiFi＋Hi-C | 显式选择 Hifiasm Hi-C 分相，或在组装后独立执行 AutoHiC scaffolding |
| HiFi＋父本和母本 reads | Trio：分别构建父母 yak 数据库，再交给 Hifiasm；可同时提供超长 ONT |
| Illumina DNA paired reads | 独立 fastp QC 和 Genome Survey，或作为长读组装的调查材料；当前没有短读段组装器 |
| clean 长读段／已有组装 | 可从 TUI 中途入口或文件 manifest 开始 |

`--ont` 不是“给 HiFi 添加 ONT”的开关。用户必须明确超长 ONT 的材料身份，再将其放入 `ultralong_ont_reads_ref`。Trio 与 Hi-C 分相是当前互斥的 Assembly 输入模式；Trio 之后仍可独立执行 AutoHiC。

## 文件配置示例

在 manifest 中分别登记子代 HiFi 和 ONT，平台分别使用 `pacbio_hifi`、`ont`。在已有 contig RunConfig 的 `assembly` 中加入：

```json
{
  "hifiasm_inputs": {
    "ultralong_ont_reads_ref": ["/data/child.ul.fastq.gz"]
  }
}
```

这些路径必须已经在同一次运行的 manifest 登记。引用原始文件选择的是材料身份；若 QC 已交付对应清洁产物，下游使用经过完整性校验的当前产物。

Trio 的父母材料单独登记，例如：

```yaml
- asset_type: raw_short_dna_reads
  paths: [/data/father.R1.fastq.gz, /data/father.R2.fastq.gz]
  platform: illumina
  format: fastq
  compression: gzip
  metadata:
    intake_role: trio_paternal_reads
- asset_type: raw_short_dna_reads
  paths: [/data/mother.R1.fastq.gz, /data/mother.R2.fastq.gz]
  platform: illumina
  format: fastq
  compression: gzip
  metadata:
    intake_role: trio_maternal_reads
```

再在 `assembly.hifiasm_inputs` 指定两组完整路径：

```json
{
  "trio_paternal_reads_ref": ["/data/father.R1.fastq.gz", "/data/father.R2.fastq.gz"],
  "trio_maternal_reads_ref": ["/data/mother.R1.fastq.gz", "/data/mother.R2.fastq.gz"]
}
```

父母 reads 不进入子代 QC/Survey 材料池。Trio 执行需要额外的 `yak`，运行冻结会检查并记录它的工具依赖。每个亲本的所有文件都进入该亲本的 k-mer 数据库。

Hi-C 分相使用 `assembly.hifiasm_inputs.hic_reads_ref` 选择 manifest 中明确配对的 Hi-C 材料。DNA 短读段与 Hi-C 的 manifest 均使用 `paired_reads: [{r1: PATH, r2: PATH}]` 表达每个 lane，不根据文件名猜测配对。选择清洗时分别设置 `read_qc.short_reads`、`read_qc.hic_reads`；每个 lane 交付 clean R1/R2 及 fastp JSON/HTML 报告。Hi-C 清洗默认保留连接位点，避免将结构信号当成普通接头处理。

## 交付与验证边界

分相运行分别保存 hap1/hap2 FASTA 和原生图，不拼接两个单倍型。当前主组装交接使用 hap1，默认下游 Dedup/Review 沿该主交接执行；两个 FASTA 的存在不代表两者均已完成独立质量评估。

注册阶段有实际执行或明确无进程处置，单元与集成测试验证契约、命令和恢复。真实工具版本适配、代表样本质量、分相准确率和独立 QV 仍须通过生物学验收判断。当前只执行本地主机工具，不提供远程调度器或通用安装器。
