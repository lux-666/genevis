<a id="documentation-map"></a>

# 文档导航

<a id="choose-the-audience-first"></a>

## 先选择读者角色

- [用户指南](user_guide/README.md)：安装、主机准备、contig 运行、评审关卡、检查与恢复。
- [开发者指南](developer_guide/README.md)：按改动类型查找代码、测试、描述符、架构契约和实现证据。
- [架构地图](architecture/README.md)：贡献者的主要设计入口；读者索引的拆分不改变或削弱任何架构权威。

这两个读者索引只负责导航，链接到同一套操作规程与架构契约，不重复定义。

<a id="fastest-reading-paths"></a>

## 最短阅读路径

操作者：

```text
README.md
  -> user_guide/README.md
     -> deployment/migration.md：新主机安装
     -> user_guide/run-and-recovery.md：run/inspect/review/resume
  -> examples/：可复制的输入
```

架构与实现工作：

```text
docs/README.md
  -> developer_guide/README.md
     -> architecture/README.md
     -> topics/system-map.md
        -> 受影响的专题
           -> 专题链接的权威参考契约
              -> langgraph-implementation-map.md：当前接线与证据
```

无需线性通读 `docs/architecture/`。专题解释子系统如何连接；参考文档定义精确字段、不变量、数组、默认值和生命周期语义。

<a id="authority-entry-points"></a>

## 权威入口

| 事项 | 权威来源 |
|---|---|
| 文档政策与变更控制 | [`architecture/reference/governance.md`](architecture/reference/governance.md) |
| 架构契约与当前实现状态 | [`architecture/README.md`](architecture/README.md) |
| Python 依赖 | [`../pyproject.toml`](../pyproject.toml) |
| 公共 CLI | [`../app/entrypoints/run.py`](../app/entrypoints/run.py) |
| 阶段注册 | [`../app/workflow.py`](../app/workflow.py) |
| 软件包与方法能力 | [`../descriptors/registry.yaml`](../descriptors/registry.yaml) |
| 当前主机映射 | `GENE_AGENT_ENVS_CONFIG`，否则使用被 Git 忽略的本机配置 `../config/envs.yaml` |
| 缺少主机配置时的种子 | [`examples/envs.yaml.example`](examples/envs.yaml.example) |
| NCBI 快照安装与文件布局 | [`../data/reference/ncbi/README.md`](../data/reference/ncbi/README.md) |
| 通过 schema 校验的操作输入 | [`examples/`](examples/) |
| 运行与恢复规程 | [`user_guide/run-and-recovery.md`](user_guide/run-and-recovery.md) |
| 新主机安装 | [`deployment/migration.md`](deployment/migration.md) |
| Mac/服务器更新规程 | [`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md) |

架构地图负责导航到各项精确契约及状态投影；文档角色、同步规则和质量门禁由治理契约维护。
