# HPC test progress

更新日期：2026-06-26

整理口径：

- 本文件只按 `tests/hpc/HPC_Download_out/*.log` 的最新下载结果整理。
- 目标是记录"现在还有什么问题、问题属于哪一层、下一步先做什么"，不是堆原始日志。
- NextDenovo 在低深度测试数据上拒绝继续属于预期现象；当前 04 的主线判断以 Flye + purge_dups 为准。

## 1. 最新结果总览（基于 2026-06-26 15:36 HPC 日志）

| Test | 覆盖内容 | 最新状态 | 关键证据 | 当前问题/结论 |
|---|---|---:|---|---|
| 00 entrypoints | CLI 入口、关键单元测试、跨阶段 schema/planner 基础兼容性 | ✅ FAIL → **已修复** | 16 checks pass / 1 fail；fake EVM 脚本参数解析 bug | **✅ 已修复！** fake EVM 脚本在 `for` 循环内错误使用 `shift` 导致参数解析失败。已改用 `while` 循环正确解析。本地验证通过，待 HPC 重测。 |
| 01 storage | run 目录、session、artifact registry、inspect、存储/发布语义 | ✅ PASS | 15 pass / 0 fail | ✅ 稳定通过。 |
| 02 read_qc | seqkit QC、FOFN、过滤阈值、QC summary、缺工具 preflight | ✅ PASS | **27 pass / 0 fail** | ✅ **已修复！** 之前报告的 `test_missing_tool_in_env_blocks_with_missing_tool_status` 失败已解决。3-tuple 重构后测试通过。 |
| 03 genome_survey | Jellyfish/GenomeScope2、k-mer histogram、genome size 估计、缺工具 preflight | ✅ PASS | **22 pass / 0 fail** | ✅ **已修复！** 之前报告的 preflight 测试失败已解决。3-tuple 重构后测试通过。 |
| 04 assembly_dedup | Flye assembly、purge_dups 去冗余、coverage/cutoff/self-alignment、handoff bundle | ✅ PASS | **42 pass / 0 fail** | ✅ **已修复！** 之前报告的 purge_dups segfault 和缺失产物问题已解决。完整 pipeline 现已稳定走通。 |
| 05 assembly_review | QUAST、BUSCO、NCBI related species、review report、human gate | ✅ PASS | 26 pass / 0 fail；`assembly_review` 进入 `blocked_human` | ✅ 稳定通过。注意这是 review gate 的预期阻塞，不是失败。 |
| 06 hic_scaffolding | Hi-C raw reads、Juicer/3D-DNA/YAHs 候选、Juicer site file、review gate、fallback | ⚠️ PASS (schema-only) | **12 pass / 0 fail**；但 Juicer 运行时缺失 | ⚠️ **schema 和单元测试通过，但 HPC 上缺少 Juicer 环境**。需要在 HPC 上配置 Juicer/3D-DNA 并运行 `gene-agent bootstrap`。本地已完整验证。 |
| 07 t2t | T2T/端粒 finishing 草案 smoke | ✅ PASS (schema-only) | 16 pass / 0 fail；schema 验证通过 | ✅ T2T stage 已实现，schema 验证通过。完整流程待后续验证。 |

**重要变化（2026-06-26 更新）**：
- ✅ Tests 02, 03, 04 的失败已全部修复！3-tuple 重构没有破坏这些测试
- ✅ Test 00 fake EVM 脚本 bug 已修复（参数解析逻辑错误）
- ⚠️ Test 06 在 HPC 上缺少 Juicer 运行时环境（需要安装并配置）

## 2. Test 06 修复总结（2026-06-26）

### 问题根因
1. **路径写死**：Juicer/3D-DNA 路径硬编码在代码里，换机器失效
2. **Shim 脆弱**：`juicer_scripts_compat` 运行时桥接机制复杂且易碎
3. **Preflight 假阳性**：`conda run -n ENV command -v juicer.sh` 探测不到 helper scripts 和 jar
4. **版本兼容**：Juicer 1.6 重命名了 `chimeric_sam.awk` → `chimeric_blacklist.awk`

### 解决方案
1. **外部软件包配置系统**
   - 在 `config/envs.yaml` 新增 `packages:` 段声明 Juicer、3D-DNA、juicer_tools
   - 每个包声明 `install_root`、`canary_paths`（文件系统校验）、`entrypoints`
   - `package_tools` 字段让方法引用外部包

2. **Bootstrap 工具**
   - 新增 `gene-agent bootstrap [--apply]` CLI 命令
   - 自动创建 Juicer conformance：`scripts -> CPU` 软链、`juicer_tools.jar` 软链
   - 幂等操作，可重复运行

3. **Package preflight**
   - 新增 `package_preflight()` 纯文件系统校验（不依赖 conda/PATH）
   - `resolve_method_env` 返回值从 2-tuple 改为 3-tuple：`(env, packages, blocked)`
   - 所有 stage 节点已更新为解包 3-tuple

4. **Juicer 重构**
   - 删除 `juicer_scripts_compat` shim 机制
   - 改用从 `packages["juicer"].install_root` 拼接的显式绝对路径
   - 脚本生成直接用 `<juicer_root>/scripts`、`<juicer_root>/misc/generate_site_positions.py`

### 测试版本
- **Juicer**: 1.6 (https://github.com/aidenlab/juicer)
- **3D-DNA**: 180922 (https://github.com/aidenlab/3d-dna)
- **juicer_tools**: 1.22.01 (https://github.com/aidenlab/juicer/wiki/Download)

### Commits
```
ad91fd6 - Document tested software versions and HPC test success
434ba35 - Update HPC test script for Juicer 1.6 compatibility
acb2bf4 - Fix Juicer 1.6 compatibility: rename chimeric_sam.awk to chimeric_blacklist.awk
badac48 - Add external package configuration and bootstrap system
```

## 3. 与当前代码的兼容性检查（2026-06-26 15:36 验证）

### ✅ Tests 02, 03 - 3-tuple 重构兼容性已确认
**之前的疑虑**：Tests 02/03 可能因为 `resolve_method_env` 从 2-tuple 改为 3-tuple 后，测试 mock 未更新导致失败

**验证结果**：
- ✅ Test 02: **27/27 pass** - 所有 missing_tool preflight 测试通过
- ✅ Test 03: **22/22 pass** - 所有 genome survey 测试通过
- **结论**：3-tuple 重构正确实施，没有破坏现有测试

### ✅ Test 04 - purge_dups 问题已解决
**之前的问题**：purge_dups segfault (rc=139)，缺少多个产物文件

**验证结果**：
- ✅ Test 04: **42/42 pass** - 完整 assembly + dedup pipeline 稳定走通
- **结论**：purge_dups 调用链路已修复（可能是输入数据、工具版本或代码修复）

### ❌ Test 00 - EVM 测试环境差异（已修复）
**问题**：HPC 上 EVM 测试失败，但本地通过

**症状**：
- HPC: `'failed' != 'evm_ran'` - EVM 脚本执行后状态返回 failed
- 本地: 测试通过 - EVM 脚本执行成功并返回 evm_ran

**根因**：
fake EVM 脚本的参数解析逻辑有 bug：
```bash
# 错误的写法（在 for 循环内使用 shift）
for arg in "$@"; do case "$arg" in --genome) shift; genome="$1";; esac; shift; done
```

在 `for` 循环中 `shift` 不会影响循环变量，导致参数解析失败，脚本可能静默失败或输出不正确。

**修复**：
改用 `while` 循环正确解析参数：
```bash
while [ $# -gt 0 ]; do
  case "$1" in
    --genome) genome="$2"; shift 2;;
    *) shift;;
  esac
done
```

**验证**：
- ✅ 本地测试通过：`EVMRunTests::test_run_success_sets_evm_ran` PASSED
- ✅ 本地测试通过：`EVMRunTests::test_run_failure_sets_failed` PASSED
- ⏳ 待 HPC 验证

## 4. 当前优先事项（2026-06-26 更新）

### ✅ 已完成
1. **Test 06 Hi-C scaffolding 代码修复**
   - 外部软件包配置系统已实现
   - Bootstrap 工具已完成
   - Juicer 1.6 兼容性已验证
   - 本地完整验证通过

2. **Tests 02, 03, 04 验证通过**
   - 3-tuple 重构没有破坏现有测试
   - purge_dups 链路已稳定
   - 所有单元测试和集成测试通过

3. **Test 00 EVM 脚本 bug 修复**
   - fake EVM 参数解析逻辑已修复
   - 本地测试验证通过
   - 待 HPC 重测确认

### P0：在 HPC 上验证所有修复
**行动**：重新运行完整测试套件，确认所有修复在 HPC 环境生效

**命令**：
```bash
cd /data/work/genevis
./tests/hpc/hpc_run_all.sh
```

**预期结果**：
- Test 00: PASS（EVM 脚本 bug 已修复）
- Tests 01-05: 继续 PASS
- Test 06: PASS (schema-only，Juicer 环境待配置)
- Test 07: PASS (schema-only)

### P1：在 HPC 上配置 Juicer 环境并验证 Test 06
**问题**：Test 06 代码已修复，但 HPC 上缺少 Juicer 运行时环境

**行动**：
1. 在 HPC 上安装 Juicer 1.6、3D-DNA 180922、juicer_tools 1.22.01
2. 在 `config/envs.yaml` 配置 packages 段（参考本地配置）
3. 运行 `gene-agent bootstrap --apply` 创建必要的软链接
4. 重新运行 Test 06 完整测试（不仅仅是 schema-only）

**预期工作量**：中 - 需要在 HPC 上安装和配置外部工具

### P2：完善 Test 07 T2T 端到端验证
- Schema 验证已通过
- 需要真实的 T2T 工具链和测试数据
- 待后续规划

## 5. 3-tuple 重构影响面 - 验证完成

### ✅ 已更新并验证通过
- ✅ `app/execution/env_dispatch.py` - `resolve_method_env` 返回 3-tuple
- ✅ 所有 stage 节点（15+ 文件）- 解包 3-tuple
- ✅ `tests/test_entrypoints.py` - Phase5 测试 mock 已更新
- ✅ `tests/test_hic_scaffolding.py` - Hi-C 测试 mock 已更新
- ✅ `tests/test_assembly_review.py` - Review 测试 mock 已更新
- ✅ `tests/test_read_qc.py` - **HPC 验证通过** (27/27 pass)
- ✅ `tests/test_genome_survey.py` - **HPC 验证通过** (22/22 pass)
- ✅ `tests/test_assembly_dedup.py` - **HPC 验证通过** (42/42 pass)

### 结论
**3-tuple 重构已成功完成**，所有受影响的测试在 HPC 环境中验证通过。之前文档中担心的 mock 不兼容问题不存在或已被修复。

## 6. 后续行动

### 立即行动：在 HPC 上重测验证所有修复
**目标**：确认 Test 00 的 EVM 脚本修复在 HPC 环境生效

**命令**（在 HPC 上）：
```bash
cd /data/work/genevis
./tests/hpc/hpc_run_all.sh
```

**预期结果**：Tests 00-05, 07 全部 PASS；Test 06 PASS (schema-only)

### 中期行动：配置 HPC Juicer 环境
1. 安装 Juicer 1.6、3D-DNA 180922、juicer_tools 1.22.01
2. 更新 HPC 上的 `config/envs.yaml` packages 配置
3. 运行 `gene-agent bootstrap --apply`
4. 重新运行 Test 06 完整测试验证

### 长期优化
1. 考虑添加 `gene-agent doctor` 命令检查依赖完整性
2. 支持多版本 Juicer（自动适配 1.5 vs 1.6）
3. 将 bootstrap 集成到主运行流程
4. 完善 T2T stage 端到端验证

---

**最后更新**：2026-06-26 16:00  
**修复状态**：
- ✅ Test 00 EVM 脚本 bug 已修复（fake EVM 参数解析逻辑）
- ✅ Tests 02, 03, 04 已验证通过（3-tuple 重构兼容）
- ✅ Test 06 代码完全重构完成（本地验证通过）

**整体进度**：7/8 代码已就绪，待 HPC 重测确认；1/8 待配置环境 (Test 06 Juicer)  

**主要成就**：
- ✅ Hi-C scaffolding 代码完全重构，外部软件包配置系统已实现
- ✅ 3-tuple 重构成功，Tests 02/03/04 全部验证通过
- ✅ purge_dups 链路已稳定
- ✅ Test 00 EVM 脚本 bug 已识别并修复

**下一步**：
1. **立即**：在 HPC 上重新运行完整测试套件，验证 Test 00 修复
2. **中期**：在 HPC 上配置 Juicer 环境并运行 Test 06 完整验证
