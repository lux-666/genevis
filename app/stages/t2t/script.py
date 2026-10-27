"""Freeze the terminal reconstruction implementation into one runner script."""

from __future__ import annotations

import ast
import shlex
import sys
from pathlib import Path
from typing import Any

from app.stages import sequences
from app.stages.t2t import algorithms, reconstruction


def reconstruction_script(config: dict[str, Any]) -> str:
    """No live repository imports at execution/resume: code is in the snapshot."""

    class RemoveLocalImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom | None:
            if node.module and (
                node.module.startswith("app.") or node.module == "__future__"
            ):
                return None
            return node

    parts = ["from __future__ import annotations", "import sys"]
    for module in (sequences, algorithms, reconstruction):
        tree = RemoveLocalImports().visit(
            ast.parse(Path(str(module.__file__)).read_text())
        )
        parts.append(ast.unparse(tree))
    parts.append(f"config = {config!r}")
    parts.append(
        "(reconstruct if sys.argv[1] == 'reconstruct' else readback_evidence)(config)"
    )
    worker = "\n".join(parts)
    marker = "GENE_AGENT_T2T_FROZEN_WORKER"
    if marker in worker:
        raise ValueError("unexpected frozen worker delimiter in inputs")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "run_worker() {",
        f"{shlex.quote(sys.executable)} - \"$@\" <<'{marker}'",
        worker,
        marker,
        "}",
    ]
    work = Path(config["work_dir"])
    lines.append(
        f"minimap2 --version > {shlex.quote(str(work / 'minimap2.version.txt'))}"
    )
    for reference, output in (
        (config["assembly"], work / "long_reads_to_assembly.paf"),
        (str(work / "reconstructed_assembly.fa"), work / "readback.paf"),
    ):
        lines.append(f": > {shlex.quote(str(output))}")
        for preset, reads in sorted(config["reads"].items()):
            lines.append(
                f"minimap2 -c -x {shlex.quote(preset)} --secondary=yes "
                f"{shlex.quote(reference)} {shlex.quote(reads)} "
                f">> {shlex.quote(str(output))}"
            )
        lines.append(
            "run_worker "
            + (
                "reconstruct"
                if output.name == "long_reads_to_assembly.paf"
                else "check"
            )
        )
    return "\n".join(lines) + "\n"
