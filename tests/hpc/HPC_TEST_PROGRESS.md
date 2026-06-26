# HPC test progress

更新日期：2026-06-26

整理口径：

- 本文件只按 `tests/hpc/HPC_Download_out/*.log` 的最新下载结果整理。
- 目标是记录"现在还有什么问题、问题属于哪一层、下一步先做什么"，不是堆原始日志。
- NextDenovo 在低深度测试数据上拒绝继续属于预期现象；当前 04 的主线判断以 Flye + purge_dups 为准。

## 1. 最新结果总览

| Test | 覆盖内容 | 最新状态 | 关键证据 | 当前问题/结论 |
|---|---|---:|---|---|
| 00 entrypoints | CLI 入口、关键单元测试、跨阶段 schema/planner 基础兼容性 | FAIL | 16 checks pass / 1 fail；`test_gene_annotation_merge.py::EVMRunTests::test_run_success_sets_evm_ran` 断言 `'failed' != 'evm_ran'` | EVM 成功路径的状态语义回归。它不是 HPC 数据问题，应该单独修 gene annotation merge/EVM 单元逻辑。 |
| 01 storage | run 目录、session、artifact registry、inspect、存储/发布语义 | PASS | 15 pass / 0 fail | ✅ 当前无阻塞。日志里有一个预期的 failed_terminal inspect 场景，但 test 自身通过。 |
| 02 read_qc | seqkit QC、FOFN、过滤阈值、QC summary、缺工具 preflight | FAIL | 26 pass / 1 fail；`test_missing_tool_in_env_blocks_with_missing_tool_status` 期望 `missing_tool`，实际 `completed` | 缺工具模拟没有让 stage 进入 `missing_tool`，说明 preflight/mock 路径或工具解析状态有漏洞。真实 QC 运行后进入 assembly route review 是合理的，但单测失败仍需修。**注意**：这可能与 3-tuple 返回值重构有关，需要检查 mock 是否已更新。 |
| 03 genome_survey | Jellyfish/GenomeScope2、k-mer histogram、genome size 估计、缺工具 preflight | FAIL | 21 pass / 1 fail；两个 preflight 单测期望 `missing_tool`，实际 `failed` | 工具缺失时的错误分类不一致：应该是可解释的 `missing_tool`，现在落成普通 `failed`。实际 genome survey 流程能产出结果，但 preflight contract 没守住。**注意**：这也可能与 3-tuple 返回值重构有关。 |
| 04 assembly_dedup | Flye assembly、purge_dups 去冗余、coverage/cutoff/self-alignment、handoff bundle | FAIL | 25 pass / 11 fail；`assembly_dedup.purge_dups` rc=139 segfault；缺 `PB.base.cov`、`PB.stat`、`cutoffs`、`self.paf.gz`、`dups.bed`、`purged_assembly.fasta`、`purged_candidate.json` | formal pipeline 仍没有稳定走通。核心问题是 purge_dups 调用/输入产物链路；不要重复 Flye，优先复用已有 assembly 单独验证 pbcstat/calcuts/purge_dups/get_seqs。 |
| 05 assembly_review | QUAST、BUSCO、NCBI related species、review report、human gate | PASS | 26 pass / 0 fail；`assembly_review` 进入 `blocked_human` | ✅ 测试通过。注意这是 review gate 的预期阻塞，不是失败。数据层信号偏弱：BUSCO 0%、target species unresolved，但这属于 review 内容，不是 test failure。 |
| 06 hic_scaffolding | Hi-C raw reads、Juicer/3D-DNA/YAHs 候选、Juicer site file、review gate、fallback | **PASS** ✅ | **22 pass / 0 fail**；`hic_scaffolding` 进入 `blocked_human`；成功产出 `chromosome_scaffolded_assembly.fasta`、`contact_map.hic`、`3ddna.assembly`、`hic_review_bundle.json` | **✅ 已修复！** 通过引入外部软件包配置系统（`config/envs.yaml` packages 段）和 `gene-agent bootstrap` 工具，解决了 Juicer 路径写死和 shim 脆弱性问题。Juicer 1.6 兼容性已验证（`chimeric_blacklist.awk`）。 |
| 07 t2t | T2T/端粒 finishing 草案 smoke | PARTIAL | schema 通过，但完整 run 未记录 | T2T stage 已实现，schema 验证通过。完整流程待后续验证。 |

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

## 3. 与当前代码的兼容性检查

### ✅ Test 01, 05：无影响
- 这些测试不涉及 `resolve_method_env`，与 3-tuple 重构无关
- 测试通过

### ⚠️ Test 02, 03：可能受 3-tuple 影响
**症状**：缺工具时期望 `missing_tool`，实际得到 `completed` 或 `failed`

**可能原因**：
1. 测试的 mock 可能还在返回 2-tuple `(env, blocked)`，但新代码期望 3-tuple `(env, packages, blocked)`
2. 解包不匹配导致状态判断错误

**检查点**：
```python
# tests/test_read_qc.py 或 tests/test_genome_survey.py
# 查找是否有类似的 mock：
with patch("app.stages.read_qc.nodes.resolve_method_env", return_value=(None, None)):
    # 应该改为：
    return_value=(None, [], None)
```

**建议修复**：
- 搜索 `tests/test_read_qc.py` 和 `tests/test_genome_survey.py` 中的 `resolve_method_env` mock
- 更新所有返回值为 3-tuple：`(None, [], {"status": "missing_tool", ...})` 或 `(None, [], None)`

### ❌ Test 04：与 3-tuple 无关
- purge_dups segfault 是工具本身或输入数据问题
- 与本次重构无关

### ⚠️ Test 00：可能受影响
- EVM 测试失败可能也与 3-tuple 有关
- 需要检查 `tests/test_gene_annotation_merge.py` 中的 mock

## 4. 当前优先事项（更新）

### P0：✅ Test 06 已修复
- 外部软件包配置系统已实现
- Bootstrap 工具已完成
- Juicer 1.6 兼容性已验证
- HPC 真实运行成功

### P1：修复 Test 02, 03 的 mock 不兼容
**问题**：测试 mock 还在返回 2-tuple，但新代码期望 3-tuple

**修复步骤**：
1. 搜索 `tests/test_read_qc.py` 中的 `resolve_method_env` mock
2. 搜索 `tests/test_genome_survey.py` 中的 `resolve_method_env` mock
3. 更新所有 mock 返回值为 3-tuple
4. 运行测试验证

**示例**：
```python
# 旧 (2-tuple):
return_value=(None, None)

# 新 (3-tuple):
return_value=(None, [], None)

# 或缺工具场景:
return_value=(None, [], {"status": "missing_tool", "reason": "...", ...})
```

### P1：统一 Test 04 的 purge_dups 链路
- 当前已有可用 assembly，不应重复装配
- 应单独验证 formal purge_dups pipeline
- 与 3-tuple 重构无关

### P2：修 Test 00 的 EVM 状态回归
- 检查 `tests/test_gene_annotation_merge.py` 是否也有 mock 不兼容
- 这不依赖 HPC 数据，可以独立修复

### P2：保留 Test 01, 05 为稳定锚点
- ✅ 这些测试当前通过，说明核心存储和 review 链路稳定

## 5. 3-tuple 重构影响面

### 已更新（✅）
- ✅ `app/execution/env_dispatch.py` - `resolve_method_env` 返回 3-tuple
- ✅ 所有 stage 节点（15+ 文件）- 解包 3-tuple
- ✅ `tests/test_entrypoints.py` - Phase5 测试 mock 已更新
- ✅ `tests/test_hic_scaffolding.py` - Hi-C 测试 mock 已更新
- ✅ `tests/test_assembly_review.py` - Review 测试 mock 已更新

### 待检查（⚠️）
- ⚠️ `tests/test_read_qc.py` - 可能有 mock 未更新（导致 Test 02 失败）
- ⚠️ `tests/test_genome_survey.py` - 可能有 mock 未更新（导致 Test 03 失败）
- ⚠️ `tests/test_gene_annotation_merge.py` - 可能有 mock 未更新（导致 Test 00 EVM 失败）

### 检查命令
```bash
# 搜索所有 resolve_method_env mock
grep -r "resolve_method_env" tests/*.py | grep "return_value"

# 查找 2-tuple 返回值
grep -r "return_value=(None, None)" tests/*.py

# 查找 2-tuple 返回值（其他变体）
grep -r "return_value=(" tests/*.py | grep -v "None, \[\], None"
```

## 6. 后续行动

### 立即行动（修复 Test 02, 03, 00）
1. 运行检查命令找出所有未更新的 mock
2. 批量更新为 3-tuple 返回值
3. 本地运行测试验证
4. Commit 修复

### 中期行动（Test 04）
1. 单独运行 purge_dups 工具调试
2. 验证输入数据格式
3. 检查是否是内存/资源问题导致 segfault

### 长期优化
1. 考虑添加 `gene-agent doctor` 命令检查依赖完整性
2. 支持多版本 Juicer（自动适配 1.5 vs 1.6）
3. 将 bootstrap 集成到主运行流程

---

**最后更新**：2026-06-26  
**Test 06 状态**：✅ PASS (22/22)  
**整体进度**：3/8 PASS (01, 05, 06)，5/8 需修复 (00, 02, 03, 04, 07)  
**主要成就**：✅ Hi-C scaffolding 链路已打通！
