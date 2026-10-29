# CC1 本地 finishing 工作交接（2026-10-29）

此目录归档本次 CC1 GapCloser 对照及 3、4 号后续流程的配置、调度脚本原文、
来源校验和、已提交决策和小型结果证据。文件复制自 WSL 样本目录；
采集时间、来源路径及 SHA-256 见 [index.json](index.json)。
这是本机执行记录，绝对路径和 session 均属于该主机。`.py.txt`、`.sh.txt`
用于审阅原始脚本；在开发机上应先阅读并适配路径，不能直接作为通用工具运行。

## 已交付代码

分支：`codex/autohic-recovery-gpu-handoff`。本次归档前的代码提交：
`bdf67db318aec8537cd75447060485c868a0808d`。

- `b0c4395`：提取 AutoHiC GPU 优先、CPU 回退的单次恢复工具，
  入口为 `scripts/autohic/`，使用说明见 `docs/architecture/topics/autohic.md`。
- `7f57cf3`：提供 AutoHiC 染色体边界切分产生空片段的上游补丁。
- `bdf67db`：仅在所选 review 动作需要候选时绑定候选，修复恢复决策校验。

本次提交只归档操作材料；不修改产品代码或正在执行的任务。

## GapCloser 五组结果

五组均从同一份 Hi-C 装配独立开始。1–3 号为 advisor 推荐参数；
4 号复用 asm20 比对并采用默认填补阈值；5 号另跑上游 PacBio 默认路径。

| 组 | minimap preset | identity / min_match | 缺口数 | N bp | 结果 |
| --- | --- | --- | ---: | ---: | --- |
| 1 | asm20 | 0.95 / 1000 | 221 | 110500 | 完成，无序列增益 |
| 2 | asm20 | 0.95 / 500 | 221 | 110500 | 完成，与 1 号相同 |
| 3 | asm20 | 0.95 / 250 | 221 | 110500 | 完成，与 1、2 号相同 |
| 4 | asm20 | 0.20 / 200 | 218 | 109000 | 完成，闭合 3 个缺口 |
| 5 | ava-pb | 0.20 / 200 | — | — | OOM，无最终装配 |

1–3 号输出文件 SHA-256 相同，与源装配序列相同（FASTA 格式有差异）。
4 号序列变化通过片段一致性验证；该验证不等于证明填补准确。
5 号失败证据记录峰值约 465.7 GiB 内存和 118.2 GiB swap。
准确的装配路径及 SHA 见 `gapcloser/results_index.json`；
来源、复用关系和参数见 `gapcloser/provenance.json`。
联合 QUAST 报告 `gapcloser/pre-polish-quast.tsv` 是 polish 前结果，不能作最终评估。

## 3、4 号后续流程

有效会话为 `cc1-post-gap3-motif`、`cc1-post-gap4-motif`，目录分别为：

```text
/home/lux/brassica_CC1/hic-kb/finishing/post-gap3
/home/lux/brassica_CC1/hic-kb/finishing/post-gap4
```

真实 work、artifacts、checkpoint 都位于各自
`data/runs/<session_id>/`，本归档没有复制大文件或 SQLite 数据库。
仅拉取此 Git 分支不能在另一台主机恢复同一个运行。

两组均保留 advisor 参数流程，指定同物种参考支持的 `TTTAGGG` motif。
参考末端重复证据见 `reference-terminal-repeat-evidence.json`。
3 号执行 T2T 后没有延伸候选，保留源装配；4 号按用户指示保留源装配，
没有执行端粒延伸计算。两组均进入 base_polish。

Racon 参数：两组 `window_length=500, error_threshold=0.3, match=3, gap=-4`；
3 号 `mismatch=-5`，4 号 `mismatch=-4`。因此最终比较包含 polish 参数差异。
实际命令见各组 `racon.run.sh.txt`，advisor 原文见 `polish-advisor-response.json`。

截至本次采集，两组 Racon 工具执行记录均为 `success=true, return_code=0`
（含前比对、Racon、后比对），但 enclosing resume Python 进程仍在运行，
尚未产生 polish advisor assessment。本次上传没有诊断该后处理耗时，
不能把工具退出成功等同于整个 stage 或 workflow 完成。
`status.json` 的 executing 时间是启动时间，并非心跳；实际阶段需结合进程、
execution record、日志及新产物核对。

10 月 28 日 17:12 已核对的候选 FASTA 快照（尚未经最终接受）：

| 指标 | 3 号候选 | 4 号候选 |
| --- | ---: | ---: |
| scaffold 数 | 13 | 13 |
| 长度 bp | 449015728 | 449015035 |
| N50 bp | 38773759 | 38773823 |
| N bp | 108010 | 107010 |
| 连续 N 缺口数 | 217 | 215 |

这里的缺口变化来自候选 FASTA 统计，不证明精度提升。最终接受、
polish 后 QUAST 和 BUSCO 尚未核实完成，不能沿用此前 purge 阶段的 BUSCO 数字。

## 调度和审计边界

`controller.py.txt` 为本样本专用脚本，存在硬编码环境和路径，未作为产品功能发布。
它通过公共 CLI 提交经 request/recommendation SHA 校验的
`follow_recommendation`，仅代执行已授权的 `run/accept/keep_source`；
遇到 `human_review`、`stop`、未知 gate 或最终 assembly_review 时停止跟进。
现有 gate schema 的 `decided_by=human` 由 notes 和 audit sidecar 明确注明
是 Codex 受委托提交，不声称用户逐条审阅。各组已提交文件归档于
`delegated_decisions/`。不要对运行中的相同 session 重复启动此脚本。

## 开发机继续工作

```bash
git fetch origin
git switch codex/autohic-recovery-gpu-handoff
git pull --ff-only origin codex/autohic-recovery-gpu-handoff
```

开发入口优先阅读 AutoHiC 说明和 review 修复；本目录提供实跑背景。
后续可排查重复 FASTQ→FASTA 转换及 Racon 后 Python 处理耗时，
它们是待调查项，本次没有实现修复。
