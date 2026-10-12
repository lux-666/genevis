"""Repeatable exact-clone gate without a network-installed scanner."""

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_BODY_NODES = 70


def test_no_large_exact_function_body_clones():
    bodies = defaultdict(list)
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            normalized = ast.Module(body=body, type_ignores=[])
            if sum(1 for _ in ast.walk(normalized)) < MIN_BODY_NODES:
                continue
            bodies[ast.dump(normalized, include_attributes=False)].append(
                f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}"
            )
    duplicates = [locations for locations in bodies.values() if len(locations) > 1]
    assert not duplicates, duplicates


def test_governance_names_current_shared_contracts():
    text = (ROOT / "docs/architecture/reference/governance.md").read_text()
    for owner in (
        "recover_or_invoke_response",
        "write_normalized_fasta",
        "app/stages/sequences.py",
        "tests/test_simplification_contract.py",
    ):
        assert owner in text
