# 07-08 HPC 集成冒烟测试

任务状态：待执行（需要 HPC + 真实数据）

## 背景

Tasks 02-06 完成了从 RepeatMasker 到 DIAMOND 的全部本地代码实现（freeze→run→collect），并通过了单元测试。但每个 task 的"本地冒烟验证"项均未完成。本 task 将所有散落的 HPC 验证项收拢到一起，按执行顺序排列，可以一次性在 HPC 上完成。

**执行前提**：须先完成"数据准备"（§1），再按 §2-§5 顺序执行各阶段。每个阶段的验证通过后，才进入下一阶段。

---

## §1 HPC 数据准备（一次性，非代码任务）

| # | 数据项 | 用途 | 来源 |
|---|---|---|---|
| D1 | 海豚基因组组装 FASTA（已有，来自 assembly 阶段 bundle） | repeat + gene annotation 输入 | HPC artifact 目录 |
| D2 | Dfam Cetacea/Laurasiatheria 子库（FASTA 格式） | RepeatMasker 数据库 | 用 `famdb.py` 从本地 Dfam 导出 |
| D3 | 参考蛋白 FASTA（哺乳类 UniProt/OrthoDB cerebrids 子集） | miniprot 证据输入 | 下载 / 已有 HPC 数据 |
| D4 | Augustus 物种模型（`cetartiodactyla` 或 `human`） | augustus 预测 | conda env 内 `/augustus/config/species/` |
| D5 | BUSCO 谱系数据集目录（`cetacea_odb10` 或 `mammalia_odb10`） | BUSCO 完整度评估 | 下载或 HPC 已有 |
| D6 | SwissProt FASTA（哺乳类子集即可） | DIAMOND blastp 功能注释 | 下载 / 已有 |

### D2 — Dfam 库导出命令（参考）

```bash
# 在 Dfam 数据库所在节点执行
famdb.py -i /path/to/Dfam.h5 families \
  --format fasta_name \
  --ancestors --descendants \
  --include-class LTR,DNA,LINE,SINE,RC \
  Cetacea > /data/db/dfam_cetacea.fasta
```

### D3 — 参考蛋白（mammal subset）

```bash
# 从 OrthoDB 哺乳类 level 下载，或从 UniProt 筛选 Cetacea
wget https://v100.orthodb.org/download/odb10_vertebrata_fasta.tar.gz  # 示例
# 解压后按需筛选 FASTA
```

### 清单 §1

- [ ] D1 海豚基因组 FASTA 路径确认（`/path/to/dolphin_assembly.fasta`）
- [ ] D2 Dfam Cetacea/Laurasiatheria FASTA 导出到 HPC（`/data/db/dfam_cetacea.fasta`）
- [ ] D3 参考蛋白 FASTA 落盘（`/data/db/mammal_proteins.fa`）
- [ ] D4 Augustus 物种模型在 conda env 内可用（`augustus --species=cetartiodactyla` 不报错）
- [ ] D5 BUSCO 谱系目录落盘（`/data/db/cetacea_odb10/`）
- [ ] D6 SwissProt FASTA 落盘（`/data/db/swissprot.fasta`）

---

## §2 Manifest Schema 缺口修复（代码改动，执行前必须合并）

### 缺口：`reference_proteins` 不在 `_ACCEPTED_MANIFEST_ASSET_TYPES` 中

`app/stages/gene_annotation/nodes.py:88` 通过 `_asset_paths(state, {"reference_proteins"})` 读取参考蛋白路径，但 `app/schemas/manifest.py:24` 的 `_ACCEPTED_MANIFEST_ASSET_TYPES` 集合里没有 `"reference_proteins"`，用户无法在 manifest YAML 中声明它。

**影响**：miniprot 的 `protein_evidence_refs` 将永远为空，即使用户在 manifest 里写了 `reference_proteins` 也会被 manifest validator 拒绝。

**修复**（小改动）：

```python
# app/schemas/manifest.py  — 在 _ACCEPTED_MANIFEST_ASSET_TYPES 集合中添加
_ACCEPTED_MANIFEST_ASSET_TYPES: set[AssetType] = {
    ...
    "reference_proteins",   # ← 添加
    ...
}
```

### 清单 §2

- [ ] 将 `"reference_proteins"` 加入 `_ACCEPTED_MANIFEST_ASSET_TYPES`（`app/schemas/manifest.py`）
- [ ] 补一个 manifest 验证测试确认该 asset_type 被接受
- [ ] 提交此修复

---

## §3 阶段一：repeat_annotation（Task 02 冒烟验证）

### Manifest YAML

```yaml
# manifests/dolphin_repeat.yaml
session_id: dolphin_repeat_smoke
assets:
  - asset_type: assembly_fasta_registered
    paths:
      - /path/to/dolphin_assembly.fasta
  - asset_type: repeat_library
    paths:
      - /data/db/dfam_cetacea.fasta
    provenance:
      source: dfam
      version: "3.8"
      lineage: Cetacea
```

### 运行命令

```bash
python -m app.entrypoints.run \
  --manifest manifests/dolphin_repeat.yaml \
  --stage repeat_annotation \
  --data-dir /data/runs/dolphin_repeat_smoke
```

### 验证点

```bash
# 1. 软屏蔽基因组存在且非空
ls -lh /data/runs/dolphin_repeat_smoke/artifacts/repeat_annotation/*/softmasked_genome.fasta

# 2. repeat_percentage 字段非空
grep -r "repeat_percentage" /data/runs/dolphin_repeat_smoke/artifacts/repeat_annotation/

# 3. mask_review gate 自动通过（检查 run_trace）
grep -r "mask_review" /data/runs/dolphin_repeat_smoke/artifacts/

# 4. bundle artifact 已写入
ls /data/runs/dolphin_repeat_smoke/artifacts/repeat_annotation/*/repeat_annotation_bundle.json
```

### 清单 §3

- [ ] repeat_annotation 阶段运行完成，exit status 0
- [ ] `softmasked_genome.fasta` 文件存在且非空（> 100 MB）
- [ ] `repeat_percentage` 字段有值（> 0）
- [ ] `run_trace` 包含 `"mask_review"` 和 `"repeat_annotation_stage"` 条目
- [ ] `repeat_annotation_bundle.json` artifact 写出
- [ ] Ledger 记录了 attempt_started + attempt_succeeded

---

## §4 阶段二：gene_annotation（Tasks 03-05 冒烟验证）

gene_annotation 阶段依赖 §3 的 `softmasked_genome.fasta`。

### Manifest YAML

```yaml
# manifests/dolphin_gene.yaml
session_id: dolphin_gene_smoke
assets:
  - asset_type: softmasked_genome
    paths:
      - /data/runs/dolphin_repeat_smoke/artifacts/repeat_annotation/*/softmasked_genome.fasta
  - asset_type: reference_proteins           # 需要 §2 修复后才能使用
    paths:
      - /data/db/mammal_proteins.fa
    provenance:
      source: orthodb
      version: "10"
      lineage: Mammalia

lineage_or_species_hints:
  species: cetartiodactyla                   # Augustus 物种模型
  busco_lineage: /data/db/cetacea_odb10      # BUSCO 谱系数据集目录
```

### 关键节点顺序

```
softmasked genome
  → miniprot (Task 04)     → candidate GFF3
  → augustus (Task 04)     → candidate GFF3
  → candidate_evidence_review gate (Task 03)
  → EVM merge (Task 05)    → merged GFF3 + predicted proteins FASTA
  → BUSCO (Task 05)        → completeness score
  → final_gene_set_review gate (Task 03)
  → commit
```

### 运行命令

```bash
python -m app.entrypoints.run \
  --manifest manifests/dolphin_gene.yaml \
  --stage gene_annotation \
  --data-dir /data/runs/dolphin_gene_smoke
```

### 验证点

```bash
# 1. miniprot GFF3 产出
ls /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/*/miniprot_hits.gff3

# 2. augustus GFF3 产出
ls /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/*/augustus_models.gff3

# 3. EVM 合并 GFF3（或单候选直通）
ls /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/*/final_gene_models.gff3

# 4. BUSCO 评估（completeness_status 非 failed）
grep -r "busco_completeness" /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/

# 5. 最终基因集 bundle 写出
ls /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/*/gene_annotation_bundle.json

# 6. predicted_proteins.fasta 非空（function annotation 的输入）
grep -r "predicted_proteins_ref\|proteins_fasta_ref" \
  /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/
```

### 清单 §4

- [ ] `miniprot_hits.gff3` 存在、基因特征数 > 0
  ```bash
  awk '$3=="gene"' /path/to/miniprot_hits.gff3 | wc -l  # 期望 > 1000
  ```
- [ ] `augustus_models.gff3` 存在、基因特征数 > 0
- [ ] EVM 或单候选直通：`final_gene_models.gff3` 存在
- [ ] `busco_completeness` 字段有值（或 `busco_completeness_status == "missing"` 时流程不中断）
- [ ] `predicted_proteins.fasta` 非空（下一阶段输入）
- [ ] `gene_annotation_bundle.json` 写出
- [ ] 四个 review gate（method_planning / candidate_evidence / merge / final）全部在 run_trace 中有记录

---

## §5 阶段三：function_annotation — DIAMOND（Task 06 冒烟验证）

function_annotation 依赖 §4 的 `predicted_proteins.fasta`。

### Manifest YAML / Provenance 注入

DIAMOND 的 database_provenance 当前由规划器（LLM planner）从 manifest 的 provenance 字段读取。需确认 `validate_database_provenance` 能接受以下格式：

```yaml
# manifests/dolphin_function.yaml
session_id: dolphin_func_smoke
assets:
  - asset_type: predicted_proteins
    paths:
      - /data/runs/dolphin_gene_smoke/artifacts/gene_annotation/*/predicted_proteins.fasta

# 规划器决策文件（按需手工提供或通过交互式 gate 确认）
# database_provenance 须包含：
#   method_name: function_annotation.candidate.diamond
#   database_source_ref: /data/db/swissprot.fasta
#   database_name: SwissProt
#   database_version: "2024-01"
#   snapshot_date: "2026-01-01"
```

### 运行命令

```bash
python -m app.entrypoints.run \
  --manifest manifests/dolphin_function.yaml \
  --stage function_annotation \
  --data-dir /data/runs/dolphin_func_smoke
```

### 验证点

```bash
# 1. diamond_hits.tsv 存在且非空
ls /data/runs/dolphin_func_smoke/artifacts/function_annotation/*/diamond_hits.tsv
wc -l /data/runs/dolphin_func_smoke/artifacts/function_annotation/*/diamond_hits.tsv

# 2. diamond_hits_ref 写入 stage_output
grep -r "diamond_hits_ref" /data/runs/dolphin_func_smoke/artifacts/

# 3. Ledger 记录 attempt_succeeded
grep -r "attempt_succeeded" /data/runs/dolphin_func_smoke/ledger/

# 4. function_annotation bundle 写出
ls /data/runs/dolphin_func_smoke/artifacts/function_annotation/*/function_annotation_bundle.json
```

### 清单 §5

- [ ] `diamond_hits.tsv` 文件存在、行数 > 0（outfmt 6 格式，12 列）
- [ ] `diamond_hits_ref` 在 stage_output 中有有效路径
- [ ] Ledger 有 `attempt_succeeded` 记录
- [ ] `function_annotation_bundle.json` 写出，`gene_function_tsv_ref` 指向真实文件

---

## §6 已知风险与注意事项

| 风险点 | 描述 | 处置 |
|---|---|---|
| `reference_proteins` manifest 缺口 | §2 修复前 miniprot 无法获取蛋白证据 | 先合并 §2 修复再执行 §4 |
| Augustus 物种模型不存在 | `cetartiodactyla` 需在 conda env 内预先安装 | 确认 `ls $AUGUSTUS_CONFIG_PATH/species/cetartiodactyla` |
| BUSCO 谱系目录为相对路径 | `freeze_gene_busco_attempt` 用 `shlex.quote` 传路径，需绝对路径 | manifest 中填绝对路径 |
| EVM 工具链不在 PATH | `EVidenceModeler` 须在 `env_geno_exec` 内可用 | `conda run -n env_geno_exec EVidenceModeler --version` |
| DIAMOND 首次运行时间 | `diamond makedb` 对 SwissProt 需 ~5 min；blastp 依蛋白数而定 | 预留时间窗口，不在登录节点运行 |
| function annotation database_provenance 注入路径 | 当前没有 manifest 原生字段传递 DIAMOND db 路径；需确认规划器如何接收该信息 | 执行前手工检查 planner decision file |

---

## 执行顺序总结

```
§1 数据准备          (操作，非代码)
  ↓
§2 schema 缺口修复    (代码改动，先 commit + merge)
  ↓
§3 repeat_annotation  (HPC 运行 + 验证)
  ↓
§4 gene_annotation    (HPC 运行 + 验证，依赖 §3 输出)
  ↓
§5 function_annotation (HPC 运行 + 验证，依赖 §4 输出)
```

完成 §3-§5 全部验证点后，Phase 7 的"纸面实现"转变为"端到端验证过的可用流水线"。
