"""Protect the extracted algorithm/report owners from graph/storage coupling."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "relative",
    [
        "app/stages/t2t/algorithms.py",
        "app/stages/assembly_review/rendering.py",
        "app/stages/finishing/observations.py",
    ],
)
def test_pure_evidence_transforms_do_not_depend_on_workflow(relative):
    tree = ast.parse(Path(relative).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(
                (
                    "app.storage",
                    "app.control",
                    "app.review",
                    "app.execution",
                    "langgraph",
                )
            ), (relative, module)
            assert not module.endswith(".nodes"), (relative, module)


def test_removed_sequence_grammar_and_dead_views_do_not_return():
    sources = {
        "app/stages/t2t/nodes.py": {
            "_iter_sequences",
            "_reverse_complement",
            "_open_text",
        },
        "app/stages/assembly/parameters/workflow.py": {
            "build_parameter_attempt_timeline"
        },
        "app/review/gates.py": {"declared_resume_fields"},
    }
    for relative, forbidden in sources.items():
        names = {
            node.name
            for node in ast.walk(ast.parse(Path(relative).read_text()))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not names & forbidden
