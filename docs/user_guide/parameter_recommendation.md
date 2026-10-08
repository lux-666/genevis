<a id="parameter-recommendation"></a>

# 参数推荐

本操作者指南说明 Gene Agent 请求方法参数时会发生什么，以及如何评审结果。精确模型 DTO、JSON 响应结构、schema 版本、持久化图和重试不变量仅由[参数推荐契约](../architecture/reference/parameter-recommendation.md)维护。子系统说明见[参数推荐](../architecture/topics/parameter-recommendation.md)。

<a id="where-recommendations-appear"></a>

## 推荐出现的位置

Assembly、AutoHiC、T2T 在执行方法确定后可请求参数。有效推荐先持久化，此时尚无执行尝试，然后通过 `parameter_approval` 门禁展示。

模型提出参数，不批准参数、不另选方法、不路由工作流、不启动进程。模型响应格式错误或不可用时拒绝，不静默回退到目录默认值。

<a id="what-the-operator-reviews"></a>

## 操作者评审内容

门禁处检查：

- 所选阶段、方法与工作流 occurrence。
- 本次尝试的精确稀疏参数映射。
- 可选模型摘要和持久化生物学依据。
- 数值是否适合目标读段、物种、基因组目标、资源和重试上下文。
- 省略值应保持为已提供事实、执行器默认还是工具默认。

提案可为空。省略键不会自动继承前次尝试；应用也不会背着操作者修正异常值。若批准值不能由工具安全表达，命令构造仍可拒绝。

<a id="approve-or-reject"></a>

## 批准或拒绝

附着运行时遵循门禁提示。冻结评审策略决定门禁人工等待还是使用有资格的前台倒计时。允许自动参数批准时，只应用绑定当前请求的精确持久推荐，不重生成或编辑映射。

进程中断后，按[运行与恢复](run-and-recovery.md)检查和恢复。后续 `review` 或 `resume` 是人工恢复，不重建过期前台计时器。

拒绝推荐不创建执行尝试。支持重试的阶段稍后重试时产生可独立评审的新推荐，其模型上下文可包含早先尝试的有界证据。

<a id="audit-evidence"></a>

## 审计证据

推荐证据存于当前工作流步骤的产物树，包含已验证输入/依据快照、精确模型请求与响应、解析推荐、解析结果、完整性 manifest。冻结执行参数前，审批绑定到该持久根。

通过会话摘要或产物索引定位当前推荐；不要原地编辑已存推荐文件。哈希、身份、阶段/方法和前驱不匹配均拒绝。

精确文件名、schema 版本、接受的响应字段、来源优先级、T2T 基序解析规则和重试链不变量不在此重复；调试或修改时使用参考契约。

<a id="compare-llm-only-and-literature-assisted-parameters"></a>

## 比较纯 LLM 与文献辅助参数

使用带检索案例的初始 Assembly `model_request.json` 及相邻 `input_snapshot.json`，先在不联系模型的情况下准备两个试验臂：

```bash
python -m app.diagnostics.parameter_recommendation_smoke \
  --model-request /path/to/model_request.json \
  --output-dir /path/to/new-prepared-pair --prepare-pair
```

检查各臂的 `model_request.json`。通过配置 LLM 评估两臂时，改用 `--paired` 并指定新输出目录。该诊断使用既有参数解析器/目录检查并保存原始响应，不批准参数或启动组装。重复成对试验与真实组装结果分开比较。精确控制变量见[推荐契约](../architecture/reference/parameter-recommendation.md#assembly-case-evidence)。

<a id="iterative-purge_dups-recommendations"></a>

## 迭代 purge_dups 推荐

在已有 RunConfig 中启用去冗余顾问（JSON 片段）：

```json
{
  "assembly_dedup": {
    "automatic_purge_approved": true,
    "threads": 8,
    "recommendation_mode": "advisor",
    "recommendation_output_method": "json_schema",
    "max_attempts": 3
  },
  "review_policy": {
    "stages": {
      "assembly_dedup": {"max_blocked_seconds": 300}
    }
  }
}
```

请求的 `assembly_dedup` occurrence 还需 `allow_local_execution: true`。按 [envs.yaml.example](../examples/envs.yaml.example) 在主机环境文件配置 `assembly_dedup.eval.quast`。顾问启用时就绪检查包括 QUAST 和 LLM 端点。现有配置保留单次运行的 `deterministic_baseline` 路径。

阶段先用 QUAST 评估原始草稿；每次获批 purge 尝试后，将原始草稿与全部完成候选一起评估，使用稳定 `source`、`attempt_N` 标签。LLM 接收完整 QUAST 表、实际执行脚本、历史配置和推荐，没有固定指标评分。缺失 BUSCO 或 k-mer 证据明确标为不可用，不阻止基于 QUAST 的推荐。

推荐可提出新尝试、采纳早先候选、保留原始草稿或请求人工判断。支持设置为 `read_depth` 与 `self_alignment` 策略、`asm5`/`asm10`/`asm20` 自比对预设、`end_only`（`get_seqs -e`）。自比对策略省略读段映射、`pbcstat`、`calcuts` 和 `purge_dups -2 -T/-c` 选项；这是一种替代配方，不代表总有更好生物学效果。

每项提案都通过既有评审系统打开 `dedup_recommendation`：

- `follow_recommendation` 应用精确持久提案。
- `accept_candidate` 显式选择已完成 `attempt_N`。
- `keep_prior_assembly` 交付原始草稿。
- `stop_human_review` 停止该 occurrence。

既有 `max_blocked_seconds` 控制确认：`-1` 无限等待，`0` 立即使用合格前台 resolver，正值等待指定秒数。超时遵循已存推荐，不生成替代提案或覆盖无效提案。前台进程退出后，后台经过时间不会产生自动审批。

`max_attempts` 接受 1–10，计数 purge 执行，不计模型调用或 QUAST 评估。最终评估仍可推荐采纳哪个已完成候选。重复配置和超预算尝试被拒绝。无效模型输出将结果选择交给操作者。执行或收集失败停止 occurrence，不静默采纳未经评审结果。

每次尝试从同一原始组装开始，使用独立目录。允许空 `dups.bed`。保留 purged FASTA、输出的 haplotig FASTA（如有）、推荐、审批、QUAST 报告和执行证据。选择早先候选只改变最终交付，不改变这些产物。

<a id="operator-owned-inputs"></a>

## 操作者拥有的输入

物种身份仅属于 manifest `user_metadata.target_species`，使用 NCBI 拉丁学名。RunConfig 不重复 `shared_parameters.species_facts.scientific_name`；运行时将 manifest 身份投影到规范推荐事实。RunConfig 可独立提供其他事实，例如 `shared_parameters.species_facts.ploidy`；该值驱动 GenomeScope 和 Hifiasm `--n-hap`，学名仍来自 manifest。明确 Hi-C 设置、重试策略、T2T 读段声明留在各自 schema 拥有的输入中。

从 [`examples/`](../examples/) 中符合 schema 的文件开始，通过正常 `gene-agent run` 边界验证。推荐和示例值不能静默成为额外 RunConfig 校验规则。
