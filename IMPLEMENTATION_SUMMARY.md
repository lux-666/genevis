# 外部软件包配置面 + Bootstrap 实现总结

## ✅ 已完成的工作

### 1. 核心架构变更

#### 1.1 环境解析器增强 (`app/execution/env_resolver.py`)
- ✅ 新增 `PackageSpec` 数据类：描述外部软件包（install_root、canary_paths、entrypoints、conform_on_bootstrap、jar_root_ref）
- ✅ `EnvResolver` 新增 `packages` 属性：从 `config/envs.yaml` 的 `packages:` 段加载
- ✅ `EnvSpec` 新增 `package_tools` 字段：方法级别声明需要的软件包
- ✅ 新增 `package_preflight()` 函数：纯文件系统校验，不依赖 conda/PATH
- ✅ 删除 `envs.local.yaml` 优先逻辑：`active_config_path()` 现在只返回 `config/envs.yaml`

#### 1.2 环境分发器增强 (`app/execution/env_dispatch.py`)
- ✅ `resolve_method_env` 返回值从 2-tuple 改为 3-tuple：`(EnvSpec | None, list[PackageSpec], dict | None)`
- ✅ conda preflight 通过后，额外运行 package preflight
- ✅ package preflight 失败时返回结构化 payload（status="missing_tool"、missing_tools 列表）
- ✅ 保留 `has_explicit_path_override` 旁路，但也对其运行 package preflight

#### 1.3 Juicer/3D-DNA 方法重构 (`app/stages/hic_scaffolding/method_application/juicer_3ddna.py`)
- ✅ 删除 `juicer_scripts_compat` shim 逻辑（lines 278-296）
- ✅ 删除 `_resolve_juicer_scripts_dir()` 函数：不再靠 `command -v` + dirname 猜测
- ✅ 删除 `_resolve_site_generator()` 函数：不再在 CONDA_PREFIX 里 find
- ✅ 改为从 `packages["juicer"].install_root` 显式拼接路径：
  - `juicer_scripts_dir = <juicer_root>/scripts`（conformance 后存在）
  - `generate_site_positions.py = <juicer_root>/misc/generate_site_positions.py`
  - `run-asm-pipeline.sh = <three_d_dna_root>/run-asm-pipeline.sh`
- ✅ 脚本生成改为用绝对路径，不依赖 PATH

#### 1.4 所有 stage 节点更新
以下 stage 的 `resolve_method_env` 调用已更新为解包 3-tuple：
- ✅ `app/stages/assembly/method_application/flye.py`
- ✅ `app/stages/assembly/method_application/nextdenovo.py`
- ✅ `app/stages/assembly_dedup/nodes.py`
- ✅ `app/stages/assembly_review/method_application/busco.py`
- ✅ `app/stages/assembly_review/method_application/quast.py`
- ✅ `app/stages/assembly_review/nodes.py`
- ✅ `app/stages/function_annotation/nodes.py`
- ✅ `app/stages/gene_annotation/nodes.py`
- ✅ `app/stages/genome_survey/method_application/genomescope2.py`
- ✅ `app/stages/genome_survey/method_application/jellyfish.py`
- ✅ `app/stages/hic_scaffolding/method_application/yahs.py`
- ✅ `app/stages/read_qc/method_application/seqkit_length_filter.py`
- ✅ `app/stages/read_qc/method_application/seqkit_stats.py`
- ✅ `app/stages/repeat_annotation/nodes.py`
- ✅ `app/stages/t2t/nodes.py`

### 2. 配置文件

#### 2.1 config/envs.yaml
- ✅ 新增顶层 `packages:` 段，声明 3 个外部软件包：
  - `juicer`: install_root、conform_on_bootstrap=true、jar_root_ref=juicer_tools
  - `three_d_dna`: install_root、entrypoints、canary_paths
  - `juicer_tools`: install_root、canary_paths
- ✅ `methods.hic_scaffolding.candidate.juicer_3ddna_juicebox` 调整：
  - `required_tools` 只保留 conda env 内工具（bwa、samtools、java）
  - 移除 juicer.sh、run-asm-pipeline.sh 等（改由 package_tools 表达）
  - 新增 `package_tools: [juicer, three_d_dna]`

#### 2.2 config/envs.yaml.example（新建）
- ✅ 模板文件，所有路径用占位符（/path/to/...、your_tool_env）
- ✅ 包含详细设置说明（4 步 setup instructions）
- ✅ 用户复制为 `config/envs.yaml` 并填写后即可使用

### 3. Bootstrap 工具

#### 3.1 app/execution/package_bootstrap.py（新建）
- ✅ `conform_juicer()`: 幂等地创建 scripts -> CPU 软链，以及 juicer_tools.jar 软链
- ✅ `preflight_all()`: 对所有声明的包运行 canary 文件系统校验
- ✅ `verify_envs_yaml()`: 检查配置文件存在性、占位符、路径有效性

#### 3.2 app/entrypoints/run.py
- ✅ 新增 `bootstrap` 子命令：`gene-agent bootstrap [--apply] [--config PATH]`
- ✅ `--apply` 旗标：不加时只校验，加上后执行部署（创建软链）
- ✅ `_run_bootstrap()` 处理器：
  1. 校验 envs.yaml 存在且配置正确
  2. 对声明 `conform_on_bootstrap: true` 的包运行 conformance
  3. 对所有包运行 preflight canary 校验
  4. 打印友好的 ✓/❌ 状态和 action 列表
  5. 退出码：全部 ok=0，任一失败=1

### 4. 测试

#### 4.1 测试修复
- ✅ `tests/test_entrypoints.py`: 修复 Phase5 测试回归
  - 所有 `resolve_method_env` mock 从返回 2-tuple `(None, None)` 改为 3-tuple `(None, [], None)`
  - 修复了 8 个 Phase5 测试（之前 2 个失败，现在全部通过）

#### 4.2 测试更新
- ✅ `tests/test_hic_scaffolding.py`:
  - `test_local_3ddna_raw_reads_runs_juicer_before_3ddna`: 断言改为检查绝对路径（不再检查 juicer_scripts_compat）
  - `test_local_3ddna_raw_reads_blocks_before_execution_without_juicer_helper_scripts`: 改为断言 package canary 缺失
  - `test_local_3ddna_raw_reads_blocks_before_execution_without_site_generator`: 改为断言 package canary 缺失
  - Fake bin helper 调整：按 conformance 后 layout 构建测试 fixture

- ✅ `tests/test_env_resolver.py`: 删除 envs.local.yaml 优先逻辑相关测试
- ✅ `tests/test_assembly_review.py`: 更新 mock return_value 为 3-tuple

#### 4.3 测试结果
```
✅ 155 tests passed (包括所有 Phase5 tests)
✅ 无回归
✅ Package 配置系统正常工作
✅ 3-tuple 返回签名在所有地方正确实现
```

### 5. 用户工作流

#### 5.1 初次设置（在新机器上）
```bash
# 1. 复制模板
cp config/envs.yaml.example config/envs.yaml

# 2. 编辑 config/envs.yaml，填写：
#    - default.fallback_env（你的 conda env 名，如 env_geno_exec）
#    - packages.juicer.install_root（Juicer repo 根目录）
#    - packages.three_d_dna.install_root（3D-DNA repo 根目录）
#    - packages.juicer_tools.install_root（juicer_tools.jar 所在目录）

# 3. 校验配置（dry-run）
gene-agent bootstrap

# 4. 执行 conformance（创建 Juicer 软链）
gene-agent bootstrap --apply
```

#### 5.2 Bootstrap 输出示例（成功）
```
Checking configuration: config/envs.yaml
✓ Configuration file is valid

Conformance: juicer
  ✓ Created symlink: scripts -> CPU
  ✓ Created symlink: scripts/common/juicer_tools.jar -> /data/work/softs/juicer_tools/juicer_tools.jar

Package preflight:
  ✓ juicer: all canary paths present
  ✓ three_d_dna: all canary paths present
  ✓ juicer_tools: all canary paths present

============================================================
✓ Bootstrap complete: all packages validated
```

#### 5.3 Bootstrap 输出示例（失败 - 占位符未填）
```
Checking configuration: config/envs.yaml

❌ Configuration issues found:
  - default.fallback_env is still a placeholder: your_tool_env
  - packages.juicer.install_root is still a placeholder: /path/to/juicer
  - packages.three_d_dna.install_root is still a placeholder: /path/to/3d-dna
  - packages.juicer_tools.install_root is still a placeholder: /path/to/juicer_tools
```

## 🎯 关键设计决策

### 1. 3-tuple 返回值
`resolve_method_env` 现在返回 `(env_spec, packages, blocked_payload)`：
- `env_spec`: conda 环境配置（原有）
- `packages`: 已 preflight 通过的 PackageSpec 列表（新增）
- `blocked_payload`: preflight 失败时的结构化错误信息（扩展）

### 2. Package preflight 独立于 conda preflight
- Conda preflight：用 `conda run -n ENV command -v TOOL` 检查 PATH 工具
- Package preflight：纯文件系统检查 `Path(install_root)/canary_path.is_file()`
- 两者都必须通过，方法才能执行

### 3. Conformance 是显式一次性操作
- 不在运行时自动执行（避免意外写文件系统）
- 由用户在设置完 envs.yaml 后运行 `gene-agent bootstrap --apply`
- 幂等：已存在的正确软链会跳过，不会重复创建

### 4. envs.local.yaml 优先逻辑已删除
- 简化配置查找路径，统一只认 `config/envs.yaml`
- 发布态用 `config/envs.yaml.example` 模板，用户本地 `config/envs.yaml` 填写
- `.gitignore` 本轮未动（用户后续可按需处理）

## 📋 未完成的计划项（可选优化）

根据原计划，以下项目未实现（非阻塞）：

### 1. HPC 测试脚本调整
- ❌ `tests/hpc/hpc_env.sh.example`: 删 HIC_JUICER_SCRIPTS_DIR 环境变量
- ❌ `tests/hpc/test_06_hic.sh`: 删除 manual Juicer preflight 逻辑（lines 223-271）
- ❌ `tests/hpc/run_all.sh`、`tests/hpc/common.sh`: 删 envs.local.yaml 引用

**原因**: 这些脚本在本地开发环境不可测试（需要真实 HPC 环境），且是 tests/hpc/ 目录下的专用脚本，不影响核心功能。可在 HPC 实际部署时按需调整。

### 2. 独立的 package preflight 单元测试文件
- ❌ `tests/test_env_dispatch.py`: 未创建（计划中是 package preflight 分流测试）

**原因**: Package preflight 的核心功能已被现有测试覆盖：
- `tests/test_hic_scaffolding.py` 测试了 package canary 缺失时的 block 行为
- `tests/test_entrypoints.py` 的 Phase5 测试验证了 3-tuple 返回值的正确解包
- 实际 bootstrap 命令手动测试通过

如需完整覆盖，可后续添加专门测试 `package_preflight()` 函数各种边界条件的单元测试。

## 🧪 验证清单

### 本地验证（已完成）
- ✅ 所有单元测试通过（155 tests passed）
- ✅ Bootstrap 命令可正常运行（dry-run 和 --apply 模式）
- ✅ 配置校验逻辑正确（占位符检测、路径存在性检查）
- ✅ 无回归（Phase5 测试修复）

### HPC 验证（待用户执行）
1. **配置设置**:
   ```bash
   cd /path/to/gene_agent
   cp config/envs.yaml.example config/envs.yaml
   # 编辑 config/envs.yaml，填写 HPC 上的实际路径
   ```

2. **Bootstrap 执行**:
   ```bash
   gene-agent bootstrap              # 验证配置
   gene-agent bootstrap --apply      # 执行 conformance
   ```

3. **真实 Hi-C 流程**:
   ```bash
   cd tests/hpc
   # 按 HPC_TEST_PROGRESS.md 运行 test_06_hic.sh
   # 预期：无 "java: command not found" 错误
   # 预期：生成 chromosome_scaffolded_assembly.fasta 和 contact_map.hic
   ```

## 📦 变更文件清单

### 新建文件
- `app/execution/package_bootstrap.py`
- `config/envs.yaml.example`

### 修改文件（核心）
- `app/execution/env_resolver.py` (+134 lines)
- `app/execution/env_dispatch.py` (+146 lines)
- `app/entrypoints/run.py` (+95 lines, bootstrap 子命令)
- `app/stages/hic_scaffolding/method_application/juicer_3ddna.py` (-130 lines, 删 shim)
- `config/envs.yaml` (+39 lines, packages 段)

### 修改文件（stage 节点 3-tuple 更新）
- `app/stages/assembly/method_application/flye.py`
- `app/stages/assembly/method_application/nextdenovo.py`
- `app/stages/assembly_dedup/nodes.py`
- `app/stages/assembly_review/method_application/busco.py`
- `app/stages/assembly_review/method_application/quast.py`
- `app/stages/assembly_review/nodes.py`
- `app/stages/function_annotation/nodes.py`
- `app/stages/gene_annotation/nodes.py`
- `app/stages/genome_survey/method_application/genomescope2.py`
- `app/stages/genome_survey/method_application/jellyfish.py`
- `app/stages/hic_scaffolding/method_application/yahs.py`
- `app/stages/read_qc/method_application/seqkit_length_filter.py`
- `app/stages/read_qc/method_application/seqkit_stats.py`
- `app/stages/repeat_annotation/nodes.py`
- `app/stages/t2t/nodes.py`

### 修改文件（测试）
- `tests/test_entrypoints.py` (Phase5 regression 修复)
- `tests/test_hic_scaffolding.py` (package preflight 断言更新)
- `tests/test_env_resolver.py` (删 local 优先逻辑测试)
- `tests/test_assembly_review.py` (mock 返回值更新)

## 🚀 后续建议

1. **在 HPC 上运行完整验证**（test_06_hic.sh 真实 Hi-C 流程）
2. **可选：添加 tests/test_env_dispatch.py**，专门测试 package_preflight 边界条件
3. **可选：更新 tests/hpc/ 脚本**，删除旧的 manual preflight 逻辑
4. **可选：更新 .gitignore**，添加 config/envs.yaml（避免误提交机器特定配置）

## ✨ 核心价值

### 问题解决
1. ✅ **HPC 路径写死问题**：通过 envs.yaml 配置化，换机器只需改配置文件
2. ✅ **Juicer shim 脆弱性**：删除运行时 compat 桥接，改为 bootstrap 一次性部署 + 显式绝对路径
3. ✅ **Preflight 假阳性**：conda preflight 只检查 PATH 工具，现在 package preflight 检查软件包闭包

### 架构改进
1. ✅ **关注点分离**：conda env 工具 vs 外部软件包，两个独立的 preflight 路径
2. ✅ **显式优于隐式**：不再靠 `command -v` + dirname 猜路径，直接从配置拼绝对路径
3. ✅ **幂等部署**：bootstrap 可重复运行，已存在的正确配置会跳过

### 用户体验
1. ✅ **友好的 CLI**：`gene-agent bootstrap` 提供清晰的 ✓/❌ 反馈和错误提示
2. ✅ **渐进式引导**：envs.yaml.example 模板 + 4 步 setup instructions
3. ✅ **安全的 dry-run**：默认不写文件系统，需显式 `--apply` 才部署

---

**实现完成时间**: 2026-06-26  
**测试状态**: ✅ All 155 tests passed  
**Bootstrap 状态**: ✅ CLI working, ready for HPC deployment
