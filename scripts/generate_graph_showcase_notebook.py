"""Generate the LangGraph showcase notebook.

The notebook is intentionally generated from this small script so the rendered
graph list stays easy to refresh as stages and method subgraphs evolve.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_PATH = ROOT / "app" / "entrypoints" / "graph_showcase.ipynb"


def markdown_cell(source: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(dedent(source).strip() + "\n")


def code_cell(source: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(dedent(source).strip() + "\n")


def build_notebook() -> nbf.NotebookNode:
    nb = nbf.v4.new_notebook()
    nb.metadata.update(
        {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
                "pygments_lexer": "ipython3",
            },
        }
    )
    nb.cells = [
        markdown_cell(
            """
            # Gene Agent LangGraph 展示

            这个 notebook 用当前代码里的 LangGraph builder 直接渲染系统父图和各个子图。
            适合在组会、交接、方案评审时快速说明：父图只负责调度，业务逻辑下沉到 stage
            subgraph，具体可执行方法再下沉到 method-application subgraph。

            > 运行环境建议使用项目的 `gene_agent` conda 环境。
            """
        ),
        markdown_cell(
            """
            ## 1. 导入构图函数

            这里notebook 展示的是当前仓库的真实 LangGraph 结构。
            """
        ),
        code_cell(
            """
            from __future__ import annotations

            import inspect
            from collections import OrderedDict

            from IPython.display import Image, Markdown, display

            from app.graph.builder import build_graph
            from app.stages.assembly.builder import build_stage as build_assembly_stage
            from app.stages.assembly.method_application import (
                build_flye_graph,
                build_nextdenovo_graph,
            )
            from app.stages.assembly_review.builder import build_stage as build_assembly_review_stage
            from app.stages.function_annotation.builder import build_stage as build_function_annotation_stage
            from app.stages.gene_annotation.builder import build_stage as build_gene_annotation_stage
            from app.stages.genome_survey.builder import build_stage as build_genome_survey_stage
            from app.stages.hic_scaffolding.builder import build_stage as build_hic_scaffolding_stage
            from app.stages.hic_scaffolding.method_application import (
                build_autohic_graph,
            )
            from app.stages.inventory.builder import build_stage as build_inventory_stage
            from app.stages.read_qc.builder import build_stage as build_read_qc_stage
            from app.stages.repeat_annotation.builder import build_stage as build_repeat_annotation_stage
            """
        ),
        markdown_cell(
            """
            ## 2. 渲染 helper

            `render_graph()` 优先使用 LangGraph 的 `draw_mermaid_png()` 展示图片；如果当前环境缺少
            PNG 渲染能力，会自动退回到 Mermaid 源码，仍然可以复制到 Mermaid Live Editor 或 Markdown
            渲染器里查看。
            """
        ),
        code_cell(
            """
            def builder_source(builder) -> str:
                try:
                    return inspect.getsourcefile(builder) or "unknown"
                except TypeError:
                    return "unknown"


            def render_graph(title: str, compiled_graph, *, xray: bool = False) -> None:
                graph = compiled_graph.get_graph(xray=xray)
                display(Markdown(f"### {title}"))
                display(Markdown(f"Nodes: `{len(graph.nodes)}` · Edges: `{len(graph.edges)}` · xray: `{xray}`"))
                try:
                    display(Image(data=graph.draw_mermaid_png()))
                except Exception as exc:  # pragma: no cover - notebook display fallback
                    display(Markdown(f"PNG render unavailable: `{type(exc).__name__}: {exc}`"))
                    display(Markdown("```mermaid\\n" + graph.draw_mermaid() + "\\n```"))


            parent_graph = build_graph(backend="memory")

            STAGE_GRAPHS = OrderedDict(
                [
                    ("inventory", build_inventory_stage),
                    ("read_qc", build_read_qc_stage),
                    ("genome_survey", build_genome_survey_stage),
                    ("assembly", build_assembly_stage),
                    ("assembly_review", build_assembly_review_stage),
                    ("hic_scaffolding", build_hic_scaffolding_stage),
                    ("repeat_annotation", build_repeat_annotation_stage),
                    ("gene_annotation", build_gene_annotation_stage),
                    ("function_annotation", build_function_annotation_stage),
                ]
            )

            METHOD_GRAPHS = OrderedDict(
                [
                    ("assembly.method.flye", build_flye_graph),
                    ("assembly.method.nextdenovo", build_nextdenovo_graph),
                    ("hic_scaffolding.method.autohic", build_autohic_graph),
                ]
            )
            """
        ),
        markdown_cell(
            """
            ## 3. 系统父图

            父图保持很薄：`route_controller` 根据 `current_stage` 调度固定 stage，
            每个 stage 返回后再回到 `route_controller`。父图不做生信方法选择，也不执行工具。
            """
        ),
        code_cell(
            """
            render_graph("Parent graph: thin stage scheduler", parent_graph, xray=False)
            """
        ),
        markdown_cell(
            """
            ## 4. Stage subgraphs

            每个 stage subgraph 拥有本阶段的输入校验、计划、执行候选、review/commit/fail/block 路径。
            """
        ),
        code_cell(
            """
            for name, builder in STAGE_GRAPHS.items():
                display(Markdown(f"Source: `{builder_source(builder)}`"))
                render_graph(f"Stage subgraph: {name}", builder(), xray=False)
            """
        ),
        markdown_cell(
            """
            ## 5. Method-application subgraphs

            目前可执行方法的公共形态是 `freeze -> run -> collect -> done`。这些 method subgraph
            被 stage subgraph 当作普通节点调用，用来隔离具体工具执行和 artifact 收集逻辑。
            """
        ),
        code_cell(
            """
            for name, builder in METHOD_GRAPHS.items():
                display(Markdown(f"Source: `{builder_source(builder)}`"))
                render_graph(f"Method subgraph: {name}", builder(), xray=False)
            """
        ),
        markdown_cell(
            """
            ## 6. 讲解要点

            - **Parent graph**：只做 stage 调度和 terminal 状态处理，保持薄。
            - **Stage subgraph**：承载阶段内的业务边界，例如 assembly 的候选方法规划、Hi-C 的 skip/scaffold 分支。
            - **Method subgraph**：承载具体可执行方法的冻结参数、运行、收集 artifact 生命周期。
            - **State 原则**：LangGraph state 保存小型控制事实；大文件、日志、指标表通过 ledger / artifact registry 引用。
            """
        ),
    ]
    return nb


def main() -> None:
    NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(build_notebook(), NOTEBOOK_PATH)
    print(NOTEBOOK_PATH)


if __name__ == "__main__":
    main()
