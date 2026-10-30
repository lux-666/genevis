<a id="mac-development-and-server-execution"></a>

# Mac 开发与服务器执行

本页拥有 Git 交接和服务器更新流程。运行/冻结/恢复契约见检出准备完成后使用的[运行与恢复](../user_guide/run-and-recovery.md)。Bootstrap、contig 就绪和代码测试边界索引见[部署入口](README.md)。

Mac 检出用于编辑、审查、轻量验证、提交和推送；Linux/服务器检出用于环境验证和真实生物信息学执行。Git 是受支持的代码传输边界；Gene Agent 没有代理内 SSH Runner、远程调度后端或 Mac 端产物收集器。

## 集成交付分支

当前开发与测试机交付统一使用 `codex/oss-alpha`。功能分支通过审查与代码门禁后合入该分支，
服务器从同名远端分支更新。`main`、历史 release 和单项修复分支不能凭名称视为包含最新功能。
更新时同时核对分支和提交；本地合并完成但尚未推送，服务器仍无法取得改动。

合并前检查工作树，并核对待交付分支的独有提交：

```bash
git status --short
git fetch origin --prune
git log --oneline origin/codex/oss-alpha..待交付分支
git merge-base --is-ancestor 待交付分支 origin/codex/oss-alpha
```

最后一条命令退出码为零表示该分支已进入远端集成历史；非零时需审查独有改动。
历史已合并只证明提交关系，仍需检查功能是否被后续替代、删除或回退。
旧协议已被现行实现替代时，在合并提交中明确记录替代代码和保留决策，不能机械恢复旧模块。
交付完成后记录远端提交号；测试机用 `git branch --show-current` 和 `git rev-parse HEAD`
核对实际版本，再执行本页的服务器更新和验证流程。旧会话的冻结脚本不会随 Git 更新自动替换。

<a id="source-boundary"></a>

## 源码边界

跟踪的项目材料包括代码、描述符、依赖声明、测试、文档、示例和辅助脚本。

服务器本地材料包括：

- 真实 FASTQ/FASTA/BAM 输入和样本元数据。
- `.env`、API 密钥和其他凭据。
- 活动 `config/envs.yaml`；只提交 `docs/examples/envs.yaml.example`。
- Conda 环境和外部包安装。
- 数据库、BUSCO lineage、模型文件、运行产物、Ledger、报告、checkpoint。
- 含私有运行输入或执行器设置的运行配置副本。

只通过[快照权威文档](../../data/reference/ncbi/README.md)获取并安装 NCBI 快照。安装器从活动主机映射的 `databases.ncbi` 解析目标根；映射非默认时使用 `--config PATH`。Assembly Review 拒绝无效或冻结后内容改变的快照。

<a id="first-pull-after-the-host-config-split"></a>

## 主机配置拆分后的首次拉取

`config/envs.yaml` 现在是忽略的机器本地状态。已有服务器检出若仍跟踪此路径，首次拉取 Git 跟踪切换提交可能删除工作树副本。拉取前先将活动映射备份到仓库外：

```bash
mkdir -p "$HOME/gene-agent-host-config-backups"
host_config_backup="$HOME/gene-agent-host-config-backups/envs.yaml.$(date +%Y%m%d%H%M%S)"
cp -p config/envs.yaml "$host_config_backup"

# 仅在外部备份存在后，将已跟踪路径恢复为干净状态。
git restore --source=HEAD --worktree -- config/envs.yaml
git pull --ff-only

cp -p "$host_config_backup" config/envs.yaml
git status --short --ignored -- config/envs.yaml
```

上述 `git restore` 只在精确主机映射已备份到检出外之后丢弃跟踪的工作副本。拉取后复制命令将映射恢复到现已忽略的路径。Git 应显示 `!! config/envs.yaml`；随后执行正常 bootstrap 预览与服务器检查。

新克隆只有 `docs/examples/envs.yaml.example`；操作者可手动创建活动文件或使用交互 `gene-agent bootstrap`。数据库或 AutoHiC 尚未安装时可跳过；bootstrap 只有在展示选定计划并收到确认后才创建。正常执行在活动映射缺失时不会静默使用示例。

<a id="mac-workflow"></a>

## Mac 工作流

```bash
git status --short
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache \
  python -m pytest -q
git diff --check
git add ...
git commit -m "..."
git push
```

只暂存目标文件。不要加入 `.env`、真实输入路径、运行数据、checkpoint、报告、凭据或缓存。小型提交 fixture 支持本地回归，但不能替代服务器真实数据运行。

<a id="server-update-workflow"></a>

## 服务器更新工作流

拉取前先检查有无活动会话。会话只能在兼容 checkpoint 表示下恢复；不兼容更新前先完成会话，或保留匹配的检出和环境。

下面普通更新流程仅适用于该主机已完成上述 `config/envs.yaml` 跟踪拆分。若 `git check-ignore config/envs.yaml` 未识别仓库忽略规则，应先执行备份与首次拉取步骤。

然后更新并验证：

```bash
git status --short
git pull --ff-only
bash scripts/server_test.sh
bash scripts/server_check.sh
```

两个脚本会自行定位仓库根，可从其他目录用绝对路径调用。

各自范围如下：

- `server_check.sh` 验证编排器前置条件、固定阶段工具/包闭包，以及规范五阶段 contig 模板使用的 BUSCO/NCBI 数据库注册表。Assembly 选择和候选就绪延迟到阶段入口。脚本不探测真实 LLM 端点或私有输入。
- `server_test.sh` 运行格式/lint/类型/代码回归/编译/diff 检查，显式排除 `real_tool`，不执行 Flye、Jellyfish、purge_dups、QUAST、BUSCO 或真实数据流水线。
- 真实 `gene-agent run` 预检在冻结前验证运行专属输入与服务。

两脚本均以零退出后，复制并填写服务器本地模板，遵循[运行与恢复](../user_guide/run-and-recovery.md)。新主机配置另见[新主机配置](migration.md)。
