# Phase 8.5 完成报告

**日期**: 2026-06-28
**状态**: 完成
**范围**: Universal resource resolver、preflight validation、TUI recovery flow，以及后续缺陷修复。

---

## 执行摘要

Phase 8.5 将 Phase 8 的 assembly resolver 泛化为共享资源解析层，并为 TUI execution plan 增加 preflight validation。

最终实现已经将 preflight 行为和 runtime 行为对齐：

- validation 会模拟本次计划中上游 stage 将产生的资源。
- runtime consumers 使用与 preflight 一致的 resolver 语义。
- gene annotation 在没有显式 unmasked fallback reason 时要求 softmasked genome。
- function annotation 要求 predicted proteins。
- TUI `provide_data` revalidation 成功后继续进入确认流程，而不是重置计划。

---

## 交付代码

### Universal Resource Resolver

文件：`app/stages/base.py`

核心 API：

```python
resolve_stage_resource(
    state,
    *,
    resource_spec,
    override=None,
)
```

解析优先级：

1. RunConfig override。
2. Preferred stage outputs。
3. reverse `STAGE_ORDER` 中的所有 stage outputs。
4. Asset registry。

Assembly consumers 直接使用 `resolve_stage_resource()` 和
`assembly_resource_spec(...)`；旧 wrapper 已删除。

### Resource Specs Registry

文件：`app/stages/resource_specs.py`

Registry 定义：

- `ResourceSpec`
- `ASSEMBLY_SPEC`
- `MASKED_ASSEMBLY_SPEC`
- `RAW_READS_SPEC`
- `CLEAN_READS_SPEC`
- `PREDICTED_PROTEINS_SPEC`
- `STAGE_REQUIREMENTS`
- `STAGE_OUTPUTS`

`STAGE_REQUIREMENTS` 驱动 preflight validation。
`STAGE_OUTPUTS` 驱动计划级上游产物模拟。

### Preflight Validation

文件：`app/stages/preflight.py`

核心 API：

```python
check_stage_prerequisites(state, stage)
validate_execution_plan(state, requested_stages)
```

Validation 按 canonical `STAGE_ORDER` 遍历 requested stages，模拟已经可运行的上游 stage outputs，并且只允许 downstream stage 依赖已经处理过的上游输出。

### TUI Integration

文件：`app/entrypoints/ask_interactive.py`

TUI 在最终执行确认前验证 selected stages。验证失败时用户可以：

1. 修改 stage selection。
2. 补充缺失数据，按 Enter 后在同一个 session 内重新验证。
3. Proceed anyway。

如果 `provide_data` revalidation 成功，流程会继续进入 confirmation，而不是清空 plan 重新选 stage。

---

## 已修复缺陷

### 1. Preflight 误拦正常端到端计划

问题：validation 只检查当前 state 中已经存在的资源，忽略本次计划中前序 stage 将产生的输出。

修复：

- 增加 planned-resource collection。
- 增加 simulated state updates。
- 跟踪 processed stages，避免 self-dependency 和循环依赖假阳性。

### 2. `repeat_annotation` preflight/runtime 不一致

问题：preflight 允许 asset registry 中的 assembly，但 runtime 使用本地 helper，只看 selected stage outputs。

修复：

- runtime 改为 `resolve_stage_resource(..., ASSEMBLY_SPEC)`。
- 删除旧的本地 assembly helper。

### 3. TUI `provide_data` 成功后仍重置 plan

问题：用户补充数据后 revalidation 成功，但流程仍然回到 stage selection。

修复：

- 只有 revalidation 仍失败时才重置。
- revalidation 成功时继续进入 confirmation。

### 4. `gene_annotation` softmask 假通过

问题：registered unmasked assembly 会让 preflight 通过，但 runtime 在没有 softmask 且没有 fallback reason 时会失败。

修复：

- 增加 contextual softmask requirement。
- 只有存在显式 unmasked fallback/skip reason 时才允许 fallback。
- 同一 plan 中的 `repeat_annotation` 可以满足 softmask requirement。

### 5. `function_annotation` 缺少 proteins requirement

问题：function annotation 没有 preflight requirements，缺少 predicted proteins 时仍会放行。

修复：

- 增加 `PREDICTED_PROTEINS_SPEC`。
- `function_annotation` 要求 gene annotation protein outputs。
- gene annotation 的 simulated outputs 增加 proteins。

### 6. preflight output map 重复维护

问题：resource collection 和 output simulation 各自维护 stage-output map，容易漂移。

修复：

- 将 stage output 声明集中到 `STAGE_OUTPUTS`。
- collection 和 simulation 都读取同一个 contract。

### 7. TUI 静默吞掉 preflight 异常

问题：广义异常捕获可能隐藏 preflight 实现 bug。

修复：

- 保留 TUI 不阻塞行为。
- 增加 debug traceback logging。

### 8. 文档漂移和死代码

问题：Phase 8.5 多份状态文档互相冲突，resolver 重构后仍残留旧 helper。

修复：

- Phase 8.5 文档合并为当前 design 和 completion report。
- 删除过期的 progress/fix/polish/final-summary 文档。
- 删除 `_final_assembly_for_annotation` 死代码 helper。

---

## 测试覆盖

重点验证命令：

```bash
conda run -n gene_agent python -m unittest \
  tests.test_preflight \
  tests.test_repeat_annotation \
  tests.test_gene_annotation \
  tests.test_ask_interactive -v
```

结果：**99 tests passed**。

覆盖场景：

- assembly resource 缺失和存在。
- 同一 plan 中上游资源满足 downstream stage。
- gene annotation 在没有 explicit fallback 时要求 softmask。
- repeat annotation 在同一 plan 中满足 gene annotation softmask requirement。
- function annotation 要求 gene annotation proteins。
- TUI `provide_data` revalidation 成功后继续 confirmation。
- repeat/gene annotation runtime 在 resolver 对齐后的行为。

额外验证：

```bash
git diff --check
conda run -n gene_agent python -m compileall -q \
  app/stages/base.py \
  app/stages/preflight.py \
  app/stages/resource_specs.py \
  app/stages/repeat_annotation/nodes.py \
  app/stages/gene_annotation/nodes.py \
  app/entrypoints/ask_interactive.py
```

两项均通过。

---

## 最终状态

Phase 8.5 已完成，可以作为后续阶段的基础。

以下内容刻意留到后续阶段：

- strict validation mode。
- 由 LLM/知识库生成 stage requirements。
- 更完整的 stage IO contract，供 runtime、preflight、docs 共享。
