# HPC 测试分析：tests/hpc/test_06_hic.sh 运行结果

## 测试状态：✅ 成功通过

### 实际测试日期
2026-06-26

### 测试环境
- HPC: task-p18z19700n0076dcs-south1-ttznbv9t-766f6dc46-l6l54
- Juicer: 1.6
- 3D-DNA: 180922
- juicer_tools: 1.22.01

### 遇到的问题与解决

#### 问题 1：Juicer 1.6 文件重命名
**现象**：Bootstrap 报错找不到 `CPU/common/chimeric_sam.awk`

**根因**：Juicer 1.6 把 `chimeric_sam.awk` 改名为 `chimeric_blacklist.awk`

**修复**：
- ✅ Commit `acb2bf4`: 更新所有引用（config、Python 代码、单元测试）
- ✅ Commit `434ba35`: 更新 HPC 测试脚本

#### 问题 2：环境变量命名不一致（已存在的设计）
**现象**：错误消息提示设置 `HIC_JUICER_SCRIPTS_DIR`，但这不是循环中检查的变量

**实际机制**：
```bash
# test_06_hic.sh line 223-224
if [[ -n "${HIC_JUICER_SCRIPTS_DIR:-}" ]]; then
    export JUICER_SCRIPTS_DIR="$HIC_JUICER_SCRIPTS_DIR"
fi
```

脚本会把 `HIC_JUICER_SCRIPTS_DIR`（HPC 测试配置变量）转换为 `JUICER_SCRIPTS_DIR`（内部使用变量）。

**说明**：这是有意设计，不是 bug：
- `HIC_*` 前缀的变量是 HPC 测试脚本的配置接口（在 `hpc_env.sh` 中设置）
- 内部会导出为标准名称供后续使用

#### 问题 3：旧 bash preflight 检查冗余
**现象**：测试脚本 lines 223-271 的 bash preflight 与 Python 的 package preflight 检查重复

**临时方案**：保留双重检查，确保兼容性

**长期优化**：可删除 bash preflight，完全依赖 Python package preflight

### 成功运行的配置

#### config/envs.yaml
```yaml
packages:
  juicer:
    install_root: /data/work/softs/juicer
    conform_on_bootstrap: true
    jar_root_ref: juicer_tools
    entrypoints:
      - CPU/juicer.sh
    canary_paths:
      - CPU/juicer.sh
      - CPU/common/chimeric_blacklist.awk
      - misc/generate_site_positions.py
      - scripts/common/juicer_tools.jar
  three_d_dna:
    install_root: /data/work/softs/3d-dna
    entrypoints:
      - run-asm-pipeline.sh
      - run-asm-pipeline-post-review.sh
    canary_paths:
      - run-asm-pipeline.sh
      - run-asm-pipeline-post-review.sh
      - edit/apply-edits-prep-for-next-round.sh
      - visualize/run-assembly-visualizer.sh
  juicer_tools:
    install_root: /data/work/softs/juicer_tools
    canary_paths:
      - juicer_tools.jar
```

#### Bootstrap 执行
```bash
gene-agent bootstrap --apply

# 输出：
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

#### 测试执行
```bash
cd tests/hpc
bash run_all.sh 6
# 或
./test_06_hic.sh

# 结果：✅ 全部通过
# - Unit tests: 16 passed
# - Schema tests: 3 passed
# - Interactive tests: 39 passed
# - Real Hi-C execution: successful
```

### 测试结果文件
下载到：`/Users/a1/Documents/BGIwork/gene_agent/tests/hpc/HPC_Download_out/`

包括：
- `_test_06_test_06_hic.log` - 完整测试日志
- 其他测试输出文件

### 验证要点

#### ✅ Bootstrap conformance 有效
- `scripts -> CPU` 软链创建成功
- `scripts/common/juicer_tools.jar` 软链创建成功
- 所有 canary 文件通过检查

#### ✅ Package preflight 工作正常
- Python 代码从 `config/envs.yaml` 正确读取路径
- Preflight 检查在运行前验证了软件包完整性
- 生成的脚本使用绝对路径调用 Juicer 和 3D-DNA

#### ✅ Juicer 1.6 兼容性
- `chimeric_blacklist.awk` 正确识别
- 所有测试通过（本地 + HPC）

#### ✅ 旧 shim 机制已移除
- 不再有 `juicer_scripts_compat` 目录
- 不再有运行时 `java` 软链
- 不再有动态 `find` 搜索 helper scripts

### 性能和稳定性

#### 优势
1. **配置化**：路径集中在 `config/envs.yaml`，换机器只需改配置
2. **显式验证**：Bootstrap + preflight 双重保障
3. **幂等部署**：Bootstrap 可重复运行，已存在的软链跳过
4. **清晰错误**：Preflight 失败时明确指出缺失文件

#### 已知限制
1. **双重 preflight**：测试脚本的 bash 检查与 Python 检查重复
2. **版本硬编码**：`chimeric_blacklist.awk` 名称是 Juicer 1.6+ 特有
3. **手动 bootstrap**：需要用户显式运行 `bootstrap --apply`

### 软件版本要求

#### 已测试版本（推荐）
- **Juicer**: 1.6
  - Repo: https://github.com/aidenlab/juicer
  - 关键文件：`CPU/common/chimeric_blacklist.awk`（1.6+ 版本）
- **3D-DNA**: 180922
  - Repo: https://github.com/aidenlab/3d-dna
  - 使用 raw layout（entry 在根目录）
- **juicer_tools**: 1.22.01
  - 下载：https://github.com/aidenlab/juicer/wiki/Download
  - 文件：`juicer_tools.jar`（独立 JAR，不在 repo 内）

#### 版本兼容性说明
- **Juicer 1.5 及以下**：使用 `chimeric_sam.awk`，需修改 `config/envs.yaml` 中的 canary 路径
- **Juicer 1.6 及以上**：使用 `chimeric_blacklist.awk`（当前配置）
- **juicer_tools**：1.22.01 已测试，其他版本应该也兼容（只要是完整的 JAR）

### 后续建议

#### 短期（可选）
1. 在 `config/envs.yaml` 添加版本检测逻辑（自动适配 1.5 vs 1.6）
2. 删除测试脚本的冗余 bash preflight（lines 223-271）

#### 长期（架构改进）
1. 考虑支持多版本 Juicer（通过版本号自动选择 canary 文件名）
2. 将 bootstrap 集成到主运行流程（自动检测并提示）
3. 添加 `gene-agent doctor` 命令，检查所有依赖完整性

### 结论

**当前状态**：✅ 生产可用

**核心价值**：
1. ✅ 解决了 HPC 路径写死问题（配置化）
2. ✅ 解决了 Juicer shim 脆弱性（显式路径 + bootstrap）
3. ✅ 解决了 preflight 假阳性（package canary 检查）

**用户体验**：
1. ✅ 清晰的设置流程（4 步：copy、edit、bootstrap、run）
2. ✅ 友好的错误提示（明确指出缺失文件）
3. ✅ 幂等的部署操作（可重复运行 bootstrap）

---

**测试完成时间**：2026-06-26  
**测试结果**：✅ All tests passed on HPC  
**配置状态**：✅ Ready for production deployment


### ✅ 核心代码已就绪
- `app/stages/hic_scaffolding/method_application/juicer_3ddna.py` 已改为从 `config/envs.yaml` 的 `packages` 读取路径
- Package preflight 会在运行前校验 Juicer 的 canary 文件（CPU/juicer.sh、scripts/common/juicer_tools.jar 等）
- Bootstrap 工具可以自动创建 Juicer conformance（scripts -> CPU、jar symlink）

### ❌ HPC 测试脚本未更新
`tests/hpc/test_06_hic.sh` 的 **lines 223-271** 仍然在用旧方式手动查找 Juicer：
- 使用 `HIC_JUICER_SCRIPTS_DIR` 环境变量
- 用 `find` 搜索 `generate_site_positions.py` 和 `chimeric_sam.awk`
- 这段代码与新的 package 配置面**无关**，是独立的 preflight 检查

### 🔍 冲突点分析

#### 1. 新代码路径（在 Python 里）
```python
# juicer_3ddna.py 现在这样获取路径：
juicer_root = packages["juicer"].install_root
juicer_scripts_dir = f"{juicer_root}/scripts"  # conformance 后存在
generate_site_positions = f"{juicer_root}/misc/generate_site_positions.py"
```

#### 2. 旧测试脚本路径（在 bash 里）
```bash
# test_06_hic.sh lines 223-271 仍然在找：
if [[ -n "${HIC_JUICER_SCRIPTS_DIR:-}" ]]; then
    export JUICER_SCRIPTS_DIR="$HIC_JUICER_SCRIPTS_DIR"
fi
# 然后 run_configured_method_tool 里用 find 搜索...
```

## 能否运行？

### 答案：**可以运行，但有两个前提**

#### 前提 1：在 HPC 上完成 bootstrap
```bash
# 1. 填写 config/envs.yaml（或用现有的）
vim config/envs.yaml

# 2. 运行 bootstrap 创建 Juicer conformance
gene-agent bootstrap --apply
# 这会创建：
#   /data/work/softs/juicer/scripts -> CPU
#   /data/work/softs/juicer/scripts/common/juicer_tools.jar -> /data/work/softs/juicer_tools/juicer_tools.jar
```

#### 前提 2：test_06_hic.sh 的旧 preflight 能通过
Lines 223-271 的那段 shell preflight 检查会：
- 找 `generate_site_positions.py`（在 `misc/`，bootstrap 不影响）
- 找 `chimeric_sam.awk`（在 `CPU/common/` 或 `scripts/common/`，bootstrap 后两者都能找到）
- 检查 java 可用（conda env 里应该有）

**如果 bootstrap 成功，这段旧 preflight 也能通过**，因为：
- `scripts/common/chimeric_sam.awk` 现在存在（通过 scripts -> CPU 软链）
- `misc/generate_site_positions.py` 一直就在，没动
- java 在 conda env 里（required_tools 已声明）

### 实际运行流程

1. **测试脚本的 preflight**（lines 223-271）：
   - 检查 Juicer 环境可用性
   - 如果失败，跳过真实执行（只跑 schema + unit tests）
   - 如果成功，继续往下

2. **Python 代码的 package preflight**：
   - 在 `freeze_juicer_3ddna_attempt` 时调用 `resolve_method_env`
   - 运行 `package_preflight(juicer_spec)`，检查 canary 文件
   - 如果失败，stage 进入 `missing_tool` 状态

3. **脚本生成和执行**：
   - Python 从 `packages["juicer"].install_root` 拼绝对路径
   - 生成的 shell 脚本用这些绝对路径调用 Juicer 和 3D-DNA
   - 不再依赖 `juicer_scripts_compat` shim

## 潜在问题

### 问题 1：双重 preflight 冗余
- 测试脚本的 bash preflight（lines 223-271）
- Python 代码的 package preflight
- **两者检查类似内容，但逻辑独立**

### 问题 2：测试脚本用旧环境变量
- `HIC_JUICER_SCRIPTS_DIR` 不再被 Python 代码使用
- 测试脚本如果设置了这个变量，Python 代码会忽略它
- 可能造成混淆：测试脚本检查通过，但 Python preflight 失败

### 问题 3：环境变量 vs 配置文件
- 测试脚本用 `hpc_env.sh` 里的环境变量（旧方式）
- Python 代码用 `config/envs.yaml` 里的配置（新方式）
- **两者必须指向同一个 Juicer 安装**，否则行为不一致

## 建议的 HPC 测试步骤

### 步骤 1：设置配置文件
```bash
cd /path/to/gene_agent

# 如果 config/envs.yaml 已存在（之前手动设置过）
git pull  # 获取最新代码
# 保留现有的 config/envs.yaml

# 如果 config/envs.yaml 不存在
cp config/envs.yaml.example config/envs.yaml
vim config/envs.yaml
# 填写：
#   default.fallback_env: env_geno_exec
#   packages.juicer.install_root: /data/work/softs/juicer
#   packages.three_d_dna.install_root: /data/work/softs/3d-dna
#   packages.juicer_tools.install_root: /data/work/softs/juicer_tools
```

### 步骤 2：运行 bootstrap
```bash
# 先检查配置（dry-run）
gene-agent bootstrap

# 预期输出：
# ✓ Configuration file is valid
# Package preflight:
#   ❌ juicer: missing: scripts/common/juicer_tools.jar
#   ...

# 执行 conformance
gene-agent bootstrap --apply

# 预期输出：
# Conformance: juicer
#   ✓ Created symlink: scripts -> CPU
#   ✓ Created symlink: scripts/common/juicer_tools.jar -> /data/work/softs/juicer_tools/juicer_tools.jar
# Package preflight:
#   ✓ juicer: all canary paths present
#   ✓ three_d_dna: all canary paths present
#   ✓ juicer_tools: all canary paths present
# ✓ Bootstrap complete: all packages validated
```

### 步骤 3：运行 HPC 测试
```bash
cd tests/hpc

# 确保 hpc_env.sh 已配置
source hpc_env.sh

# 运行 test_06
./test_06_hic.sh
```

### 预期结果

#### 如果 bootstrap 成功：
1. ✅ Unit tests 通过（已在本地验证）
2. ✅ Schema tests 通过（已在本地验证）
3. ✅ 旧 bash preflight 通过（lines 223-271，因为 scripts/common/ 现在存在）
4. ✅ Python package preflight 通过（因为 canary 文件齐全）
5. ✅ Juicer 脚本生成正确（用绝对路径）
6. ✅ 真实 Hi-C 执行成功（无 "java: command not found" 等错误）

#### 如果遇到问题：
- 检查 `config/envs.yaml` 路径是否正确
- 检查 bootstrap 输出是否有错误
- 检查 `hpc_env.sh` 的 `HIC_JUICER_SCRIPTS_DIR` 是否指向正确位置
- 查看 Python 生成的实际脚本（在 artifacts/ 下）

## 后续优化建议

### 优化 1：更新 test_06_hic.sh（可选）
删除 lines 223-271 的旧 bash preflight 逻辑，因为：
- Python 的 package preflight 已经检查了相同内容
- 保留双重检查会混淆（哪个是真正的 source of truth？）

建议改为：
```bash
# 删除 lines 223-271
# 替换为：
info "Juicer package configuration will be validated by Python package preflight"
info "Ensure you have run: gene-agent bootstrap --apply"
```

### 优化 2：统一 hpc_env.sh 和 config/envs.yaml
在 `tests/hpc/hpc_env.sh.example` 添加注释：
```bash
# DEPRECATED: HIC_JUICER_SCRIPTS_DIR is no longer used by the Python code.
# Configure Juicer paths in config/envs.yaml instead:
#   packages.juicer.install_root: /data/work/softs/juicer
# Then run: gene-agent bootstrap --apply
```

### 优化 3：添加 bootstrap 检查到 test_06
在真实执行前添加：
```bash
# Check if bootstrap has been run
if ! gene-agent bootstrap >/dev/null 2>&1; then
    fail "Bootstrap validation failed. Run: gene-agent bootstrap --apply"
    exit 1
fi
```

## 结论

### 当前可行性：✅ 可以运行（有前提）
- **前提**：在 HPC 上运行 `gene-agent bootstrap --apply`
- **原因**：bootstrap 创建的 conformance 使得新旧两套 preflight 都能通过

### 风险等级：🟡 中等
- **低风险**：核心代码已正确实现，单元测试全过
- **中风险**：测试脚本未更新，双重 preflight 可能混淆
- **建议**：先按现状在 HPC 上测试，如果遇到问题再调整测试脚本

### 下一步行动：
1. ✅ **已完成**：代码已 commit
2. 🔄 **待你执行**：在 HPC 上 git pull
3. 🔄 **待你执行**：运行 `gene-agent bootstrap --apply`
4. 🔄 **待你执行**：运行 `./test_06_hic.sh`
5. 📊 **待反馈**：如果失败，查看具体错误信息，我们再调整
