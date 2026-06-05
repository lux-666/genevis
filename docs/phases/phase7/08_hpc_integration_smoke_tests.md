# 08 HPC 集成冒烟测试

任务状态：待执行（需要 HPC、真实 FASTQ、真实数据库路径）

## 目标

这个 task 用真实 HPC 数据验证 Phase 7 注释执行链路是否已经接入当前主流程。测试不再使用旧的 `--stage repeat_annotation/gene_annotation/function_annotation` 单阶段入口；当前真实运行链路是：

```bash
gene-agent run -> blocked_human -> gene-agent inspect/review -> gene-agent resume -> done
```

本轮 smoke 必须覆盖：

- 两个 Actinopterygii 物种。
- 每个物种各两个 raw long-read FASTQ 文件。
- `assembly_review` BUSCO 使用 `actinopterygii_odb10`。
- `gene_annotation` 和 `function_annotation` 都通过 run-config 纳入同一条 raw FASTQ 主链。
- `function_annotation` 使用 DIAMOND，并由 run-config 提供 database provenance。

通过标准是两个物种都跑到 `done/completed`，并且两个物种的 `route_trace` 都包含完整链路：

```text
inventory_stage -> read_qc_stage -> genome_survey_stage -> assembly_stage
-> assembly_review_stage -> hic_scaffolding_stage -> repeat_annotation_stage
-> gene_annotation_stage -> function_annotation_stage
```

---

## 1. HPC 数据布局

建议单独建目录，避免复用旧 smoke 输出：

```bash
export PHASE7_ROOT=/data/work/genevis/data/phase7_08_actino_smoke
export BUSCO_ACTINO=/data/work/genevis/db/busco/actinopterygii_odb10

mkdir -p \
  "${PHASE7_ROOT}/inputs/species_a" \
  "${PHASE7_ROOT}/inputs/species_b" \
  "${PHASE7_ROOT}/db/repeats" \
  "${PHASE7_ROOT}/db/proteins" \
  "${PHASE7_ROOT}/db/function" \
  "${PHASE7_ROOT}/manifests" \
  "${PHASE7_ROOT}/run_configs" \
  "${PHASE7_ROOT}/out"
```

目标布局：

```text
${PHASE7_ROOT}/
  inputs/
    species_a/
      species_a.reads.1.fastq.gz
      species_a.reads.2.fastq.gz
    species_b/
      species_b.reads.1.fastq.gz
      species_b.reads.2.fastq.gz
  db/
    repeats/actinopterygii_repeat_library.fa
    proteins/actinopterygii_reference_proteins.fa
    function/actinopterygii_function_db.faa
  manifests/
    species_a.manifest.yaml
    species_b.manifest.yaml
  run_configs/
    species_a.run_config.json
    species_b.run_config.json
  out/
```

数据准备清单：

- [ ] `species_a` 有两个真实 raw long-read FASTQ。
- [ ] `species_b` 有两个真实 raw long-read FASTQ。
- [ ] 两个物种的 `genome_size_estimate` 已确认，单位为 bp。
- [ ] `actinopterygii_odb10` BUSCO lineage 目录已落盘。
- [ ] RepeatMasker 可用的 Actinopterygii repeat library 已落盘。
- [ ] miniprot 可用的 Actinopterygii 参考蛋白 FASTA 已落盘。
- [ ] DIAMOND 可用的功能注释 FASTA 已落盘。

---

## 2. 环境预检

```bash
conda run -n gene_agent gene-agent --help
conda run -n env_geno_exec seqkit version
conda run -n env_geno_exec flye --version
conda run -n env_geno_exec quast.py --version
conda run -n env_geno_exec busco --version
conda run -n env_geno_exec RepeatMasker -version
conda run -n env_geno_exec miniprot --version
conda run -n env_geno_exec augustus --version
conda run -n env_geno_exec diamond version
```

BUSCO lineage 必须真实存在：

```bash
test -d "${BUSCO_ACTINO}"
test -s "${BUSCO_ACTINO}/dataset.cfg"
```

FASTQ 快速检查：

```bash
conda run -n env_geno_exec seqkit stats \
  "${PHASE7_ROOT}/inputs/species_a/species_a.reads.1.fastq.gz" \
  "${PHASE7_ROOT}/inputs/species_a/species_a.reads.2.fastq.gz" \
  "${PHASE7_ROOT}/inputs/species_b/species_b.reads.1.fastq.gz" \
  "${PHASE7_ROOT}/inputs/species_b/species_b.reads.2.fastq.gz"
```

---

## 3. Manifest 模板

每个物种一个 manifest。两个 FASTQ 文件放在同一个 `raw_long_reads` asset 下，表示同一物种同一测序来源的多个输入文件。

`species_a.manifest.yaml`：

```yaml
session_id: phase7-08-species-a-YYYYMMDD-a
sample_id: species_a
target_assembly_level: contig
genome_size_estimate: 800000000

assets:
  - asset_type: raw_long_reads
    sample_id: species_a
    platform: ont
    layout: single
    format: fastq
    compression: gzip
    paths:
      - /data/work/genevis/data/phase7_08_actino_smoke/inputs/species_a/species_a.reads.1.fastq.gz
      - /data/work/genevis/data/phase7_08_actino_smoke/inputs/species_a/species_a.reads.2.fastq.gz
    metadata:
      source: hpc_phase7_08_smoke
      taxon_group: Actinopterygii

  - asset_type: repeat_library
    format: fasta
    paths:
      - /data/work/genevis/data/phase7_08_actino_smoke/db/repeats/actinopterygii_repeat_library.fa
    metadata:
      source: operator_supplied
      taxon_group: Actinopterygii

  - asset_type: reference_proteins
    format: fasta
    paths:
      - /data/work/genevis/data/phase7_08_actino_smoke/db/proteins/actinopterygii_reference_proteins.fa
    metadata:
      source: operator_supplied
      taxon_group: Actinopterygii
```

`species_b.manifest.yaml` 同样结构，只替换：

- `session_id`
- `sample_id`
- `genome_size_estimate`
- 两个 FASTQ 路径

当前 manifest schema 已接受 `reference_proteins`，不需要再做旧文档中的 schema 修复。

---

## 4. Run Config 模板

现在 `local_exec` 可以包含 `gene_annotation` 和 `function_annotation`，所以两个物种的 smoke 都应把最后两个 stage 纳入同一份 run-config。

`function_annotation.database_provenance[*].input_proteins_ref` 必须精确等于 gene stage 传给 function stage 的 `proteins_fasta_ref`。当前 gene planning 的预测蛋白路径是可预测的：

```text
${DATA_DIR}/work/gene_annotation/${SESSION_ID}/proteins.fa
```

因此每个物种的 run-config 需要按自己的 `data-dir` 和 `session-id` 填入该绝对路径。如果流程最后失败为 `protein artifact not found for function_annotation`，说明最后两 stage 已经被 config 纳入并路由到了 function execution，但 gene stage 没有物化 `proteins.fa`；这应作为 smoke 暴露的实现问题处理。

`species_a.run_config.json`：

```json
{
  "local_exec": [
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation"
  ],
  "requested_stages": [
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation"
  ],
  "shared_parameters": {
    "read_type": "ont_raw",
    "estimated_genome_size": 800000000
  },
  "read_qc": {
    "length_filter": {
      "method": "skip",
      "approved": false,
      "thresholds": {}
    }
  },
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 800000000,
      "threads": 48
    }
  },
  "assembly_review": {
    "quast": {
      "threads": 24
    },
    "busco": {
      "lineage_path": "/data/work/genevis/db/busco/actinopterygii_odb10",
      "lineage_name": "actinopterygii_odb10",
      "lineage_version": "odb10",
      "lineage_source": "operator_supplied",
      "mode": "genome",
      "threads": 32
    },
    "max_reruns": 3
  },
  "review_policy": {
    "mode": "strict"
  },
  "function_annotation": {
    "database_provenance": [
      {
        "method_name": "function_annotation.candidate.diamond",
        "database_name": "Actinopterygii functional protein database",
        "database_version": "operator_supplied",
        "snapshot_date": "2026-06-04",
        "database_source_ref": "/data/work/genevis/data/phase7_08_actino_smoke/db/function/actinopterygii_function_db.faa",
        "descriptor_version": "diamond.yaml",
        "input_proteins_ref": "/data/work/genevis/data/phase7_08_actino_smoke/out/species_a/work/gene_annotation/phase7-08-species-a-YYYYMMDD-a/proteins.fa",
        "command": "gene-agent function_annotation DIAMOND smoke"
      }
    ]
  }
}
```

`species_b.run_config.json` 同样结构，只替换：

- `shared_parameters.estimated_genome_size`
- `assembly.flye_parameters.estimated_genome_size`
- `function_annotation.database_provenance[0].input_proteins_ref`
- 必要时替换 `assembly.flye_parameters.read_type`
- 线程数按队列资源调整

---

## 5. 当前真实运行链路

### 5.1 初始 run

两个物种分别跑，session ID 必须和 manifest 中一致：

```bash
conda run -n gene_agent gene-agent run \
  --manifest "${PHASE7_ROOT}/manifests/species_a.manifest.yaml" \
  --run-config "${PHASE7_ROOT}/run_configs/species_a.run_config.json" \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --checkpointer sqlite
```

```bash
conda run -n gene_agent gene-agent run \
  --manifest "${PHASE7_ROOT}/manifests/species_b.manifest.yaml" \
  --run-config "${PHASE7_ROOT}/run_configs/species_b.run_config.json" \
  --session-id phase7-08-species-b-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_b" \
  --checkpointer sqlite
```

预期：初始 run 在 strict review policy 下停在 `assembly_review`，`supervision_state` 为 `blocked_human`，而不是报错。

### 5.2 Inspect

```bash
conda run -n gene_agent gene-agent inspect \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --json
```

必须确认：

- `current_stage` 是 `assembly_review`。
- `next_action.kind` 是 `review_decision_required`。
- `latest_review_request_artifact` 指向 `assembly_review_request.json`。
- review request 中候选 assembly 的 `final_assembly_ref` 是存在的本地文件。
- assembly-review artifact 中存在 QUAST summary。
- assembly-review artifact 中存在 BUSCO summary，且 lineage 是 `actinopterygii_odb10`。

### 5.3 Review + resume

TTY 场景优先使用：

```bash
conda run -n gene_agent gene-agent review \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a"
```

review 时选择：

- assembly review：`accept` 已执行的 assembly candidate。
- annotation intake：保留 `repeat_annotation -> gene_annotation -> function_annotation`。
- gene methods：至少选择 `gene_annotation.candidate.miniprot`。如要同时覆盖 Augustus/EVM，选择 `miniprot + augustus`，并确认 Augustus species model 在执行环境可用。
- function methods：保留 DIAMOND provenance；DIAMOND 是否实际执行由 `function_annotation.database_provenance` 中的 `function_annotation.candidate.diamond` 记录决定。

自动化场景使用模板文件：

```bash
conda run -n gene_agent gene-agent inspect \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --write-decision-template "${PHASE7_ROOT}/out/species_a/decision_assembly_review.json"
```

编辑 decision 文件，至少保留：

```json
{
  "stage": "assembly_review",
  "decision": "accept",
  "candidate_id": "<candidate id from inspect>",
  "final_assembly_ref": "<existing final_assembly_ref from inspect>",
  "decided_by": "human",
  "notes": ["phase7 08 actinopterygii smoke approved"]
}
```

然后 resume：

```bash
conda run -n gene_agent gene-agent resume \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --decision-file "${PHASE7_ROOT}/out/species_a/decision_assembly_review.json" \
  --checkpointer sqlite
```

`species_b` 同理。后续如果再次进入 gene annotation 或其他 review gate，用同一套 `inspect -> review` 或 `inspect -> decision file -> resume` 链路继续，不要改回 `--stage` 命令。

---

## 6. 强制验收点

每个物种都必须满足以下检查。

### 6.1 终态

```bash
conda run -n gene_agent gene-agent inspect \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --json
```

通过标准：

- [ ] `supervision_state` 为 `completed`。
- [ ] `current_stage` 为 `done`。
- [ ] `route_trace` 包含：
  - `inventory_stage`
  - `read_qc_stage`
  - `genome_survey_stage`
  - `assembly_stage`
  - `assembly_review_stage`
  - `hic_scaffolding_stage`
  - `repeat_annotation_stage`
  - `gene_annotation_stage`
  - `function_annotation_stage`
- [ ] `out/checkpoints/checkpoints.sqlite` 存在。
- [ ] `out/ledger/<SESSION>/events.jsonl` 存在。
- [ ] `out/artifacts/final_report/<SESSION>/review_decision_report.json` 存在。

### 6.2 FASTQ 输入被真实消费

```bash
ls -lh "${PHASE7_ROOT}/out/species_a/artifacts/read_qc/phase7-08-species-a-YYYYMMDD-a/read_qc_summary.json"
grep -n "read_qc" "${PHASE7_ROOT}/out/species_a/ledger/phase7-08-species-a-YYYYMMDD-a/events.jsonl"
```

通过标准：

- [ ] `read_qc_summary.json` 存在。
- [ ] summary 能追溯到两个 FASTQ 输入。
- [ ] ledger 中 `read_qc` 有 `method_attempt_started` 和成功记录。

### 6.3 Assembly Review + BUSCO

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/assembly/phase7-08-species-a-YYYYMMDD-a" \
  -maxdepth 4 -type f \( -name "*.fasta" -o -name "*.fa" \)

find "${PHASE7_ROOT}/out/species_a/artifacts/assembly_review/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "*busco*" -o -name "short_summary*" -o -name "quast_summary.json" \)
```

通过标准：

- [ ] Flye draft assembly FASTA 存在且非空。
- [ ] QUAST summary 存在。
- [ ] BUSCO 输出存在。
- [ ] BUSCO lineage/provenance 指向 `actinopterygii_odb10`。
- [ ] BUSCO 没有因为缺 lineage、缺 tool、缺 dataset 被跳过。

### 6.4 Repeat Annotation

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/repeat_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "*softmasked*" -o -name "*repeat*" -o -name "*bundle*.json" \)
```

通过标准：

- [ ] RepeatMasker 或当前选择的 repeat 方法有真实 attempt 记录。
- [ ] softmasked genome 产物存在。
- [ ] repeat annotation bundle 存在。

### 6.5 Gene Annotation

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/gene_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "*.gff3" -o -name "*.faa" -o -name "*.fa" -o -name "*bundle*.json" -o -name "*review*.json" \)
```

通过标准：

- [ ] `gene_annotation` 的 stage output 中 `execution_mode` 为 `local`。
- [ ] `miniprot` 有真实 attempt 记录，或 review/plan 中明确记录未选择原因。
- [ ] 若选择 Augustus/EVM，相关 GFF3 和 merge 产物存在。
- [ ] `gene_annotation_bundle.json` 存在。
- [ ] `proteins_fasta_ref` 指向 `run_config.function_annotation.database_provenance[0].input_proteins_ref`。
- [ ] `proteins_fasta_ref` 对应文件真实存在；否则 DIAMOND 无法通过。

### 6.6 Function Annotation + DIAMOND

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/function_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "diamond_hits.tsv" -o -name "*bundle*.json" -o -name "*plan*.json" \)
```

通过标准：

- [ ] `function_annotation` 的 stage output 中 `execution_mode` 为 `local`。
- [ ] `function_annotation_plan.json` 存在。
- [ ] database provenance 通过校验，没有 `missing database_provenance`。
- [ ] `diamond makedb` 和 `diamond blastp` 有 attempt 记录。
- [ ] `diamond_hits.tsv` 存在。
- [ ] `function_annotation_bundle.json` 存在，并记录 DIAMOND database provenance。
- [ ] final report 存在。

---

## 7. 双物种通过标准

整轮 task 只有在两个物种都满足以下条件时才算通过：

- [ ] `species_a` 从两个 FASTQ 跑到 `done/completed`。
- [ ] `species_b` 从两个 FASTQ 跑到 `done/completed`。
- [ ] 两个物种的 `local_exec` 都包含 `gene_annotation` 和 `function_annotation`。
- [ ] 两个物种的 assembly-review BUSCO 都使用 `actinopterygii_odb10`。
- [ ] 两个物种都有 durable sqlite checkpoint，并且 resume 后没有重复执行同一 method attempt。
- [ ] 两个物种都有 repeat annotation bundle。
- [ ] 两个物种都有 gene annotation bundle。
- [ ] 两个物种都有 function annotation bundle。
- [ ] 两个物种都有 final report。
- [ ] 两个物种的 ledger 中 human review decision 至少包含 assembly review 的 `accept`。

重复 attempt 检查可用以下 Python 片段分别对两个物种运行：

```bash
conda run -n gene_agent python - <<'PY'
import json
from collections import Counter
from pathlib import Path

ledger = Path("/data/work/genevis/data/phase7_08_actino_smoke/out/species_a/ledger/phase7-08-species-a-YYYYMMDD-a/events.jsonl")
events = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
attempts = [
    f"{event.get('stage')}/{event.get('method_name')}"
    for event in events
    if event.get("event_type") == "method_attempt_started"
]
dupes = {key: count for key, count in Counter(attempts).items() if count > 1}
if dupes:
    raise SystemExit(f"duplicate method attempts after resume: {dupes}")
print("PASS: no duplicate method attempts")
PY
```

---

## 8. 失败解释

这轮 smoke 把最后两个 stage 纳入强制验收，因此以下失败都应记录为待修问题，而不是从 smoke 中排除：

- `missing database_provenance for function_annotation`：run-config 没有正确传入 `function_annotation.database_provenance`。
- `database_provenance[*].input_proteins_ref does not match proteins_fasta_ref`：run-config 中预测蛋白路径填错。
- `protein artifact not found for function_annotation`：gene stage 没有物化 `proteins.fa`，导致 DIAMOND 无法运行。
- `diamond` attempt failed：DIAMOND database、执行环境或输入蛋白 FASTA 有问题。
- `busco_lineage_not_supplied` 或 `busco_lineage_path_missing`：assembly-review BUSCO 配置没有正确指向 `actinopterygii_odb10`。

---

## 9. 记录到 handoff

执行完成后，在 `docs/handoff.md` 追加一段 Phase 7 task 08 run record，至少包含：

- HPC host / working directory。
- git commit。
- 两个 session ID。
- 两个 manifest 路径。
- 两个 run-config 路径。
- 两个物种的 `local_exec` 列表。
- `actinopterygii_odb10` lineage 路径。
- 每个物种的 terminal inspect 摘要。
- 每个物种的 assembly-review QUAST/BUSCO artifact refs。
- 每个物种的 repeat/gene/function annotation bundle refs。
- 每个物种的 DIAMOND hits ref。
- 每个物种的 final report ref。
