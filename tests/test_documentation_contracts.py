"""Small contracts preventing documentation and server-helper drift."""

from __future__ import annotations

import re
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    return [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").rglob("*.md"))]


def _markdown_lines(text: str) -> list[tuple[int, str]]:
    """Keep source locations while excluding fenced examples from link checks."""
    result = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            result.append((number, line))
    return result


def _markdown_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in _markdown_lines(text):
        anchors.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', line))
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        title = re.sub(r"<[^>]*>", "", heading[1]).lower()
        slug = "".join(
            char
            for char in title
            if char in " -_" or unicodedata.category(char)[0] in "LN"
        ).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


class DocumentationContractTests(unittest.TestCase):
    def test_implementation_map_method_inventory_matches_the_registry(self) -> None:
        from app.schemas.descriptors import DescriptorRegistry

        registry = DescriptorRegistry.load(PROJECT_ROOT / "descriptors/registry.yaml")
        document = (
            PROJECT_ROOT / "docs/architecture/langgraph-implementation-map.md"
        ).read_text()
        declared = re.search(r"包含 (\d+) 个注册方法：(\d+) 个 `local`", document)
        self.assertIsNotNone(declared)
        assert declared is not None
        self.assertEqual(int(declared[1]), len(registry.methods))
        local_count = sum(
            method.planner.execution_kind == "local"
            for method in registry.methods.values()
        )
        self.assertEqual(int(declared[2]), local_count)
        inventory = document.split("## 注册方法清单", 1)[1].split("## 参数能力映射", 1)[
            0
        ]
        documented = set(re.findall(r"^\| `([\w.]+)` \|", inventory, re.MULTILINE))
        self.assertEqual(documented, set(registry.methods))

    def test_markdown_relative_links_resolve(self) -> None:
        broken: list[str] = []
        anchor_cache: dict[Path, set[str]] = {}
        for source in _markdown_files():
            for line_number, line in _markdown_lines(
                source.read_text(encoding="utf-8")
            ):
                for raw_target in MARKDOWN_LINK.findall(line):
                    target = raw_target.strip().strip("<>")
                    if re.match(r"^[a-zA-Z][\w+.-]*:", target):
                        continue
                    relative, _, fragment = target.partition("#")
                    relative = unquote(relative.split("?", 1)[0])
                    resolved = (
                        (source.parent / relative).resolve() if relative else source
                    )
                    location = source.relative_to(PROJECT_ROOT)
                    if not resolved.exists():
                        broken.append(f"{location}:{line_number}: {target}")
                    elif fragment and resolved.suffix == ".md":
                        if resolved not in anchor_cache:
                            anchor_cache[resolved] = _markdown_anchors(
                                resolved.read_text(encoding="utf-8")
                            )
                        if unquote(fragment) not in anchor_cache[resolved]:
                            broken.append(f"{location}:{line_number}: {target}")
        self.assertFalse(
            broken, "broken relative Markdown links:\n" + "\n".join(broken)
        )

    def test_active_env_config_is_ignored_and_has_a_tracked_template(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("\nconfig/envs.yaml\n", f"\n{gitignore}")
        self.assertTrue(
            (PROJECT_ROOT / "docs" / "examples" / "envs.yaml.example").is_file()
        )

    def test_existing_host_config_split_has_safe_pull_sequence(self) -> None:
        migration = (
            PROJECT_ROOT / "docs" / "deployment" / "mac_dev_server_pull.md"
        ).read_text(encoding="utf-8")

        backup = migration.index('cp -p config/envs.yaml "$host_config_backup"')
        clean = migration.index(
            "git restore --source=HEAD --worktree -- config/envs.yaml"
        )
        pull = migration.index("git pull --ff-only", clean)
        restore = migration.index('cp -p "$host_config_backup" config/envs.yaml')
        self.assertLess(backup, clean)
        self.assertLess(clean, pull)
        self.assertLess(pull, restore)

    def test_server_helpers_locate_repo_root_and_keep_code_gates_in_order(self) -> None:
        server_check = (PROJECT_ROOT / "scripts" / "server_check.sh").read_text(
            encoding="utf-8"
        )
        server_test = (PROJECT_ROOT / "scripts" / "server_test.sh").read_text(
            encoding="utf-8"
        )
        for script in (server_check, server_test):
            self.assertIn("BASH_SOURCE[0]", script)
            self.assertIn("REPO_ROOT=", script)
            self.assertIn('cd "${REPO_ROOT}"', script)

        gates = (
            "ruff format --check .",
            "ruff check .",
            "mypy app",
            "pytest -q -m 'not real_tool'",
            "compileall app tests",
            "git diff --check",
        )
        positions = [server_test.index(gate) for gate in gates]
        self.assertEqual(positions, sorted(positions))
