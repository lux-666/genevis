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


def test_ledger_and_parameter_storage_do_not_import_business_owners():
    for relative in ("app/storage/ledger.py", "app/storage/parameter_evidence.py"):
        tree = ast.parse(Path(relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("app.review", "app.llm", "app.stages", "app.entrypoints")
                ), (relative, node.module)


def test_session_control_has_no_terminal_presentation_dependency():
    for relative in ("app/control/session.py", "app/control/stage_seeds.py"):
        tree = ast.parse(Path(relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app.entrypoints")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print"
