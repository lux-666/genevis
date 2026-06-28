# Phase 8 & 8.5: 资源解析架构

**日期**: 2026-06-28
**状态**: 完成

---

## 范围

Phase 8 和 Phase 8.5 统一了 genome workflow 中各 stage 发布、发现、验证上游资源的方式。

- **Phase 8**: 中心化 assembly 解析逻辑，并梳理 stage output contract。
- **Phase 8.5**: 将 assembly resolver 泛化为 universal resource resolver，并为 TUI 增加 preflight validation。

当前实现支持端到端计划、用户自带数据、stage 内产物传递，以及 TUI 中缺失数据后的不退出式恢复。

---

## 当前主文档

- [PHASE_8.5_DESIGN.md](PHASE_8.5_DESIGN.md) - 当前架构、API、契约和设计决策。
- [PHASE_8.5_COMPLETE.md](PHASE_8.5_COMPLETE.md) - 完成报告、缺陷修复和验证结果。
- [DESIGN_SUMMARY.md](DESIGN_SUMMARY.md) - Phase 8 / 8.5 的高层设计总结和经验教训。

原先用于记录进度、polish 计划、最终总结、修复日志的 Phase 8.5 过程文档已经合并进 `PHASE_8.5_COMPLETE.md`。

---

## Phase 8 交付内容

- 中心化 assembly resolver。
- 显式 T2T manual review gate。
- 统一 stage output contract，兼容当前 key 和 legacy key。
- 重构 downstream assembly consumers。
- 支持用户自带 assembly 的 Scenario 4。
- 覆盖 Hi-C scaffolding、T2T、repeat annotation、gene annotation 的回归测试。

---

## Phase 8.5 交付内容

- `app/stages/base.py` 中的 universal resource resolver。
- `app/stages/resource_specs.py` 中的 resource specs registry。
- `app/stages/preflight.py` 中的 preflight validation API。
- `app/entrypoints/ask_interactive.py` 中的 TUI 集成。
- repeat、gene、function annotation 的 runtime/preflight 输入契约对齐。
- preflight validation 中的计划级上游资源模拟。
- preflight 行为和 TUI provide-data recovery 的回归测试。

---

## Stage 模型

```text
inventory
read_qc
genome_survey
assembly
assembly_dedup
assembly_review
hic_scaffolding
t2t
repeat_annotation
gene_annotation
function_annotation
```

---

## 核心架构

### Layer 1: TUI / Plan

TUI 在最终确认执行前验证用户选择的 stage plan。

```python
validation = validate_execution_plan(
    state=state,
    requested_stages=selected_stages,
)
```

验证失败时，用户可以：

1. 修改 stage selection。
2. 补充缺失数据，并在同一个 TUI session 中重新验证。
3. 对高级/调试场景选择 proceed anyway。

### Layer 2: RunConfig

Stage-specific config 可以覆盖自动解析结果。

```python
local_exec = {
    "hic_scaffolding": {
        "assembly_fasta_ref": "/path/to/custom.fa",
    }
}
```

### Layer 3: Stage Runtime

Stage runtime 通过共享 resource spec 解析输入资源。

```python
resolution = resolve_stage_resource(
    state,
    resource_spec=ASSEMBLY_SPEC,
    override=config.get("assembly_fasta_ref"),
)
```

解析优先级：

1. RunConfig override。
2. Preferred stage outputs。
3. reverse `STAGE_ORDER` 中的所有 stage outputs。
4. Asset registry。

---

## Runtime Contract

Resource specs 位于 `app/stages/resource_specs.py`。

主要 specs：

- `ASSEMBLY_SPEC`
- `MASKED_ASSEMBLY_SPEC`
- `RAW_READS_SPEC`
- `CLEAN_READS_SPEC`
- `PREDICTED_PROTEINS_SPEC`

`STAGE_REQUIREMENTS` 声明每个 stage 的 preflight requirements。
`STAGE_OUTPUTS` 声明每个 stage 可模拟产生的资源 key。

requirements 和 simulated outputs 放在同一个 registry 中，可以减少 validation 和 runtime 之间的 drift。

---

## 验证快照

最新重点验证命令：

```bash
conda run -n gene_agent python -m unittest \
  tests.test_preflight \
  tests.test_repeat_annotation \
  tests.test_gene_annotation \
  tests.test_ask_interactive -v
```

结果：**99 tests passed**。

额外检查：

- `git diff --check`
- 对修改过的 stage / entrypoint 模块执行 `compileall`

---

## Archive

历史设计讨论位于 [archive/](archive/)：

- `ASSEMBLY_RESOLUTION_REFACTOR.md`
- `RETHINK_RESOLVER_SCOPE.md`
- `WHY_UPSTREAM_NO_RESOLVER.md`
- `STAGE_OUTPUT_CONTRACT.md`
- `REFACTOR_COMPLETE.md`
- `DESIGN_ISSUES_AND_TODO.md`
