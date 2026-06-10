# 08 Phase 7 端到端验收与 HPC 冒烟测试

任务状态：待执行。文档已重构为验收契约；需要 HPC、真实 FASTQ、真实数据库路径。

## 目标

本任务不是单个功能切片，而是 Phase 7 的验收层。它验证 repeat、gene、function 三段 annotation chain 是否能在主流程中真实运行，并通过 durable checkpoint + review/resume 到达最终 `done/completed`。

主链入口固定为：

```bash
gene-agent run
  -> blocked_human
  -> gene-agent inspect/review
  -> gene-agent resume
  -> done
```

不再使用旧的 `--stage repeat_annotation/gene_annotation/function_annotation` 单阶段入口。

## 验收分层

### 08A: 本地缩小版 E2E

本地 E2E 使用 fake tools 和小输入，目标是快速防止路由、checkpoint、review 和 artifact contract 退化。

必须覆盖：

- durable sqlite checkpointer
- assembly review accept
- repeat_annotation -> mask_review
- gene_annotation candidate execution
- final gene set review
- function_annotation DIAMOND handoff
- final report
- route trace 完整
- resume 后不重复执行同一 method attempt

本地 E2E 不要求真实生物结果，但必须验证所有 bundle 中的路径都是真实存在的文件，不能是 planning expected output 占位。

### 08B: HPC 真数据 smoke

HPC smoke 使用两个 Actinopterygii 物种、真实 raw long-read FASTQ、真实 BUSCO lineage、真实 repeat library、真实 reference proteins 和真实 DIAMOND database。

它验证工具环境、数据库 provenance、生物输入规模和长流程稳定性。

## 通过标准

两个物种都必须达到：

- `current_stage == "done"`
- `supervision_state == "completed"`
- `route_trace` 包含完整链路：

```text
inventory_stage
read_qc_stage
genome_survey_stage
assembly_stage
assembly_review_stage
hic_scaffolding_stage
repeat_annotation_stage
gene_annotation_stage
function_annotation_stage
```

并且以下 artifact 均存在：

- durable sqlite checkpoint
- ledger events
- repeat annotation bundle
- gene annotation bundle
- function annotation bundle
- final report

## Phase 7 当前契约基线

08 必须验证以下契约，而不是复制旧任务里的路径假设。

### Repeat Annotation

当前真实执行边界：

- RepeatMasker
- provided repeat library
- softmasked genome
- repeat GFF3
- mask_review accept/skip

当前不强制：

- EDTA
- RepeatModeler
- TRF
- RepeatMasker 内置 `-species` Dfam 路径

### Gene Annotation

当前真实执行边界：

- Miniprot candidate execution
- AUGUSTUS candidate execution when `augustus_species_model` is available
- CandidateBundle
- EVM merge or single-candidate passthrough
- gffread final proteins/transcripts materialization
- BUSCO on final materialized proteins/transcripts
- final gene set review

08 不允许 gene bundle 或 BUSCO 使用不存在的 planning expected output 路径。

Miniprot native `--gff` 必须作为 `OTHER_PREDICTION` / `--gene_predictions` 进入 EVM。它不能直接作为 `PROTEIN` / `--protein_alignments`，除非本轮 smoke 使用的是已验证转换器产出的 Miniprot alignment GFF3。

### Function Annotation

当前真实执行边界：

- DIAMOND makedb + blastp
- database provenance validation
- function annotation bundle

08 应优先在 provenance 中省略 `input_proteins_ref`，让 function annotation 使用 gene bundle 中实际发布的 `proteins_fasta_ref` 并回填 provenance。只有在专门测试路径匹配时，才显式写 `input_proteins_ref`。

## 1. HPC 数据布局

建议单独建目录：

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
- [ ] Miniprot 可用的 Actinopterygii reference proteins FASTA 已落盘。
- [ ] DIAMOND 可用的 function protein database FASTA 已落盘。

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
conda run -n env_geno_exec gffread --version
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

## 3. Manifest 模板

每个物种一个 manifest。两个 FASTQ 文件放在同一个 `raw_long_reads` asset 下。

`species_a.manifest.yaml`:

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

## 4. Run Config 模板

重点：不要预填 gene stage 将来生成的 proteins 路径。`function_annotation.database_provenance[*].input_proteins_ref` 可以省略，validation 会采用 gene annotation 实际发布的 proteins ref 并回填。

`species_a.run_config.json`:

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
  "gene_annotation": {
    "lineage_or_species_hints": {
      "busco_lineage": "/data/work/genevis/db/busco/actinopterygii_odb10"
    }
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
        "snapshot_date": "2026-06-10",
        "database_source_ref": "/data/work/genevis/data/phase7_08_actino_smoke/db/function/actinopterygii_function_db.faa",
        "descriptor_version": "diamond.yaml",
        "command": "diamond makedb + diamond blastp smoke"
      }
    ]
  }
}
```

`species_b.run_config.json` 同样结构，只替换：

- `shared_parameters.estimated_genome_size`
- `assembly.flye_parameters.estimated_genome_size`
- session-specific command notes if desired
- 线程数按队列资源调整

若要同时覆盖 AUGUSTUS，必须确保 inventory/hints 能解析出可用的 `augustus_species_model`。否则在 method review 中不要选择 AUGUSTUS，让 Miniprot 单候选 passthrough 路径成为本轮 smoke 的主路径。

`gene_annotation.lineage_or_species_hints` 会被入口写入 `stage_outputs.gene_annotation`，供 gene BUSCO 和 AUGUSTUS readiness 读取。若要覆盖 AUGUSTUS，可在同一对象中加入 `"augustus_species_model": "<installed_augustus_species>"`；不要把它写入 method parameters，因为 runtime 不从该通道读取。

## 5. 运行流程

### 5.1 初始 run

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

预期：strict review policy 下停在 `assembly_review`，`supervision_state` 为 `blocked_human`。

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

TTY 场景：

```bash
conda run -n gene_agent gene-agent review \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a"
```

review 选择建议：

- assembly review: accept 已执行的 assembly candidate。
- annotation intake: 保留 `repeat_annotation -> gene_annotation -> function_annotation`。
- mask review: accept softmasked genome，除非 RepeatMasker 明显异常。
- gene methods: 至少选择 `gene_annotation.candidate.miniprot`。
- AUGUSTUS: 只有在 `augustus_species_model` 可用时选择。
- final gene set review: 若 BUSCO 缺失或较低，记录原因后人工决定是否继续。
- function methods: 保留 DIAMOND provenance。

自动化场景可用 decision template：

```bash
conda run -n gene_agent gene-agent inspect \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --write-decision-template "${PHASE7_ROOT}/out/species_a/decision_assembly_review.json"
```

然后：

```bash
conda run -n gene_agent gene-agent resume \
  --session-id phase7-08-species-a-YYYYMMDD-a \
  --data-dir "${PHASE7_ROOT}/out/species_a" \
  --decision-file "${PHASE7_ROOT}/out/species_a/decision_assembly_review.json" \
  --checkpointer sqlite
```

后续 review gate 均使用同一套 `inspect -> review` 或 `inspect -> decision file -> resume` 链路。

## 6. 强制验收点

以下检查每个物种都要执行。

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
- [ ] `route_trace` 包含完整 stage 链路。
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
- [ ] ledger 中 `read_qc` 有真实 attempt 记录。

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
- [ ] assembly-review BUSCO 输出存在。
- [ ] assembly-review BUSCO lineage/provenance 指向 `actinopterygii_odb10`。
- [ ] assembly-review BUSCO 没有因为缺 lineage、缺 tool、缺 dataset 被跳过。

### 6.4 Repeat Annotation

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/repeat_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "*softmasked*" -o -name "*repeat*" -o -name "*bundle*.json" \)
```

通过标准：

- [ ] RepeatMasker 有真实 attempt 记录。
- [ ] `softmasked_genome_ref` 文件存在且非空。
- [ ] repeat annotation GFF3 存在。
- [ ] repeat annotation bundle 存在。
- [ ] mask review decision 记录在 ledger 和 artifact 中。

### 6.5 Gene Annotation

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/gene_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "*.gff3" -o -name "*.faa" -o -name "*.fa" -o -name "*bundle*.json" -o -name "*review*.json" \)
```

通过标准：

- [ ] 至少一个 candidate method 有真实 attempt 记录，默认 Miniprot。
- [ ] CandidateBundle 引用的 GFF3 文件存在。
- [ ] Miniprot native GFF 未进入 EVM `--protein_alignments`。
- [ ] final GFF3 存在。
- [ ] final proteins FASTA 存在且非空。
- [ ] BUSCO 使用 final proteins 或 final transcripts。
- [ ] `gene_annotation_bundle.json` 存在。
- [ ] bundle 中不存在未落盘的 expected output 占位路径。

### 6.6 Function Annotation + DIAMOND

```bash
find "${PHASE7_ROOT}/out/species_a/artifacts/function_annotation/phase7-08-species-a-YYYYMMDD-a" \
  -type f \( -name "diamond_hits.tsv" -o -name "*bundle*.json" -o -name "*plan*.json" \)
```

通过标准：

- [ ] function annotation resolved proteins ref 等于 gene bundle 的 `proteins_fasta_ref`。
- [ ] database provenance 通过校验。
- [ ] provenance 中省略 `input_proteins_ref` 时已回填 resolved proteins ref。
- [ ] `diamond makedb` 和 `diamond blastp` 有 attempt 记录。
- [ ] `diamond_hits.tsv` 存在且非空。
- [ ] `function_annotation_bundle.json` 存在。
- [ ] bundle 不把 DIAMOND hits 冒充 GO/KEGG/Pfam/InterPro 输出。

### 6.7 Resume 幂等性

分别对两个物种运行：

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

## 7. 失败解释

以下失败都应记录为待修问题，不能从 smoke 中排除：

- `protein artifact not found for function_annotation`: gene stage 没有物化 final proteins。
- `database_provenance[*].input_proteins_ref does not match proteins_fasta_ref`: run-config 写了旧路径或错误路径。
- `missing database_provenance for function_annotation`: run-config 未提供 DIAMOND provenance。
- `diamond` attempt failed: DIAMOND database、环境或输入 proteins 有问题。
- `busco_lineage_not_supplied` 或 `busco_lineage_path_missing`: BUSCO lineage 配置错误。
- `augustus_species_model` missing: 选择了 AUGUSTUS 但没有 readiness 所需 hints。
- bundle 包含不存在路径: materialization/commit 契约回退到了 planning 占位。

## 8. 记录到 handoff

执行完成后，在 `docs/handoff.md` 追加 Phase 7 task 08 run record，至少包含：

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
- 是否发现重复 method attempts。
