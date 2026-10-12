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
    def test_acceptance_guide_orders_small_slices_before_full_route(self) -> None:
        guide = (PROJECT_ROOT / "docs/user_guide/run-and-recovery.md").read_text()
        headings = (
            "### 1. 审计并补齐单元与集成覆盖",
            "### 2. 对关键阶段运行小型真实工具冒烟",
            "### 3. 首个真实端到端：小型五阶段 contig 路线",
            "### 4. 独立验证 Hi-C 端到端切片",
            "### 5. 独立验证精修阶段",
            "### 6. 最后才运行完整真实端到端",
            "## 完整 HiFi/Hi-C 服务器验收",
        )
        positions = [guide.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        for requirement in (
            "真实工具检查必须显式选择",
            "Phase4ReadQCRealSmokeTests",
            "已完成 AutoHiC 尝试不会再次启动",
            "assembly_fasta_registered",
            "workflow_steps_from_run_config",
            "## 独立切片服务器操作手册",
            "--compare-hic-execution-from",
            "复用该运行证据",
            "不声称待办测试或服务器运行已经执行",
        ):
            self.assertIn(requirement, guide)
        self.assertNotIn("--route", guide)

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

    def test_assembly_case_contract_names_compact_live_boundary(self) -> None:
        contract = (
            PROJECT_ROOT / "docs/architecture/reference/casepack.md"
        ).read_text()
        for required in (
            "实时目录、模型投影与所有权",
            "重复表示",
            "CaseContent",
            "有意对称",
            "test_assembly_case_consumption.py",
            "project_method_choice_case()",
            "project_parameter_case()",
            "check_assembly_projection.sh",
            "_recover_assembly_method_artifact()",
            "parameter_prompt_bundle_name()",
            "唯一维护的案例库",
            "parameter_eligible",
            "一次性导入工具",
        ):
            self.assertIn(required, contract)

        corpus = PROJECT_ROOT / "descriptors/knowledge_packs/assembly"
        for retired in ("selection.json", "parameter_eligibility.json"):
            self.assertFalse(
                (corpus / retired).exists(),
                f"{retired} duplicates delivered case ownership",
            )

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

    def test_documentation_authority_map_has_required_roles(self) -> None:
        authority_map = (PROJECT_ROOT / "docs" / "README.md").read_text(
            encoding="utf-8"
        )
        required = (
            "../pyproject.toml",
            "../app/entrypoints/run.py",
            "../app/workflow.py",
            "../descriptors/registry.yaml",
            "../config/envs.yaml",
            "examples/envs.yaml.example",
            "../data/reference/ncbi/README.md",
            "examples/",
            "architecture/README.md",
            "architecture/reference/governance.md",
            "user_guide/run-and-recovery.md",
            "deployment/migration.md",
            "deployment/mac_dev_server_pull.md",
        )
        missing = [entry for entry in required if entry not in authority_map]
        self.assertFalse(missing, f"documentation authority map is missing: {missing}")

    def test_ncbi_reference_authority_owns_multi_snapshot_examples(self) -> None:
        authority = (
            PROJECT_ROOT / "data" / "reference" / "ncbi" / "README.md"
        ).read_text(encoding="utf-8")
        for required in (
            "reference_dir: /home/lux/databases/ncbi",
            "Metazoa.ncbi.tsv",
            "Viridiplantae.ncbi.tsv",
            "../../../docs/architecture/reference/stage-contracts.md#6-assembly-review",
        ):
            self.assertIn(required, authority)

    def test_governance_is_the_single_repository_engineering_policy(self) -> None:
        governance = (
            PROJECT_ROOT / "docs" / "architecture" / "reference" / "governance.md"
        ).read_text(encoding="utf-8")

        self.assertIn("仓库内部工程规程的唯一权威", governance)
        stale = [
            str(path.relative_to(PROJECT_ROOT))
            for path in _markdown_files()
            if "AGENTS.md" in path.read_text(encoding="utf-8")
        ]
        self.assertFalse(stale, f"deleted AGENTS.md is still cited by: {stale}")

    def test_audience_indexes_preserve_one_architecture_entry(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        docs_map = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        developer_index = (
            PROJECT_ROOT / "docs" / "developer_guide" / "README.md"
        ).read_text(encoding="utf-8")
        user_index = (PROJECT_ROOT / "docs" / "user_guide" / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("## Architecture at a Glance", readme)
        self.assertIn("docs/developer_guide/README.md", readme)
        self.assertIn("docs/user_guide/README.md", readme)
        self.assertIn("architecture/README.md", docs_map)
        self.assertIn("developer_guide/README.md", docs_map)
        self.assertIn("user_guide/README.md", docs_map)
        self.assertIn("../architecture/README.md", developer_index)
        self.assertIn("导航层", developer_index)
        self.assertIn("仅为索引", user_index)

    def test_active_env_config_is_machine_local_and_fail_closed(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        bootstrap_topic = (
            PROJECT_ROOT / "docs" / "architecture" / "topics" / "bootstrap.md"
        ).read_text(encoding="utf-8")
        deployment_map = (PROJECT_ROOT / "docs" / "deployment" / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("\nconfig/envs.yaml\n", f"\n{gitignore}")
        self.assertTrue(
            (PROJECT_ROOT / "docs" / "examples" / "envs.yaml.example").is_file()
        )
        self.assertIn("被 Git 忽略的机器本地状态", bootstrap_topic)
        self.assertIn("绝不回退到示例", bootstrap_topic)
        self.assertIn("必须保持忽略/未跟踪", deployment_map)

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
        self.assertIn("不会静默使用示例", migration)

    def test_readme_and_deployment_do_not_regrow_a_second_run_contract(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("## How To Run It", readme)
        self.assertNotIn("## Inspect / Review / Resume", readme)
        self.assertNotIn("## Adding a Registered Method Capability", readme)
        self.assertIn("docs/user_guide/run-and-recovery.md", readme)

        operator_guide = "../user_guide/run-and-recovery.md"
        for relative_path in (
            "docs/deployment/migration.md",
            "docs/deployment/mac_dev_server_pull.md",
        ):
            document = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(operator_guide, document)
            self.assertNotIn("requested_stages: null", document)
            self.assertNotIn("allow_local_execution", document)

    def test_topics_link_to_normative_owners_instead_of_copying_defaults(self) -> None:
        explanatory_documents = [
            *sorted((PROJECT_ROOT / "docs" / "architecture" / "topics").glob("*.md")),
            PROJECT_ROOT / "docs" / "architecture" / "langgraph-implementation-map.md",
        ]
        forbidden = (
            "review_policy.max_blocked_seconds: 300",
            "defaults to `300`",
            "默认值为 `300`",
            "默认 300",
            "read_qc -> genome_survey -> assembly -> assembly_dedup",
        )
        violations: list[str] = []
        for document in explanatory_documents:
            text = document.read_text(encoding="utf-8")
            for phrase in forbidden:
                if phrase in text:
                    violations.append(
                        f"{document.relative_to(PROJECT_ROOT)} copies {phrase!r}"
                    )
        self.assertFalse(
            violations,
            "normative values or arrays escaped their reference owner:\n"
            + "\n".join(violations),
        )

    def test_cross_cutting_semantics_stay_with_reference_owners(self) -> None:
        reference_dir = PROJECT_ROOT / "docs" / "architecture" / "reference"
        execution = (reference_dir / "execution-lifecycle.md").read_text(
            encoding="utf-8"
        )
        run_data = (reference_dir / "data-contract.md").read_text(encoding="utf-8")
        resources = (reference_dir / "resource-handoff.md").read_text(encoding="utf-8")
        parameters = (reference_dir / "parameter-recommendation.md").read_text(
            encoding="utf-8"
        )
        stages = (reference_dir / "stage-contracts.md").read_text(encoding="utf-8")
        shared = (reference_dir / "shared-facts.md").read_text(encoding="utf-8")
        conformance = (reference_dir / "conformance.md").read_text(encoding="utf-8")
        governance = (reference_dir / "governance.md").read_text(encoding="utf-8")

        self.assertIn("只接受精确", execution)
        self.assertIn('解析 JSON 对象 `{"ready": true}`', execution)
        self.assertNotIn('{"ready": true}', resources)
        self.assertIn("ReviewPolicyConfig.max_blocked_seconds", run_data)
        self.assertNotIn("max_blocked_seconds", parameters)
        self.assertNotIn("max_blocked_seconds", stages)
        self.assertIn("manifest 拥有的目标物种", shared)
        self.assertNotIn("目标物种有两套冻结表示", conformance)
        self.assertIn("审查发现编号只是在当前审查中定位问题的坐标", governance)

        for document in reference_dir.glob("*.md"):
            self.assertIsNone(
                re.search(r"\| F[1-9][0-9]* \|", document.read_text(encoding="utf-8")),
                f"audit finding IDs must not become architecture in {document.name}",
            )

    def test_server_helpers_locate_repo_root_and_document_scope(self) -> None:
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

        self.assertIn(
            "orchestrator prerequisites, configured fixed tool/package closure, "
            "and host databases",
            server_check,
        )
        self.assertIn('CALLER_CWD="$(pwd -P)"', server_check)
        self.assertIn('ENV_CONFIG_OVERRIDE="${GENE_AGENT_ENVS_CONFIG:-}"', server_check)
        self.assertIn(
            'ENVS_CONFIG="${CALLER_CWD}/${ENV_CONFIG_OVERRIDE}"', server_check
        )
        self.assertIn("LLM endpoint", server_check)
        self.assertIn("real inputs", server_check)
        self.assertIn("BUSCO/NCBI database registry", server_check)
        self.assertIn(
            "Assembly method selection: deferred to stage entry", server_check
        )
        self.assertIn("stage-owned method selection", server_check)
        self.assertNotIn("ORCHESTRATOR_ENV", server_check)
        self.assertNotIn("conda run", server_check)
        self.assertIn("code regression and compile checks only", server_test)
        self.assertIn("not a real-data or biological-tool smoke test", server_test)
        self.assertIn("pytest -q -m 'not real_tool'", server_test)
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
        self.assertNotIn("unittest discover", server_test)
        self.assertNotIn("npx", server_test)
        self.assertNotIn("conda run", server_test)

    def test_server_check_derives_tools_without_hardcoded_tool_closure(self) -> None:
        server_check = (PROJECT_ROOT / "scripts" / "server_check.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs/examples/contig_run_config.template.json", server_check)
        self.assertIn("effective_method_closure", server_check)
        self.assertIn("preflight_run_readiness", server_check)
        self.assertNotIn("EXEC_TOOLS=", server_check)
        for executable in (
            "seqkit",
            "jellyfish",
            "genomescope2",
            "flye",
            "minimap2",
            "pbcstat",
            "calcuts",
            "purge_dups",
            "quast.py",
            "busco",
        ):
            self.assertNotIn(executable, server_check)

    def test_deployment_entrypoints_do_not_claim_bootstrap_implies_contig_ready(
        self,
    ) -> None:
        deployment_map = (PROJECT_ROOT / "docs" / "deployment" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("映射内每个方法项", deployment_map)
        self.assertIn("固定阶段方法/包闭包", deployment_map)
        self.assertIn("Assembly 方法选择延迟", deployment_map)
        self.assertIn("不证明 `server_check.sh` 一定成功", deployment_map)

    def test_host_guides_run_code_gate_before_host_gate(self) -> None:
        commands = {
            "README.md": (
                "bash scripts/server_test.sh",
                "bash scripts/server_check.sh",
            ),
            "docs/deployment/README.md": (
                "bash /path/to/gene_agent/scripts/server_test.sh",
                "bash /path/to/gene_agent/scripts/server_check.sh",
            ),
            "docs/deployment/migration.md": (
                "bash /path/to/gene_agent/scripts/server_test.sh",
                "bash /path/to/gene_agent/scripts/server_check.sh",
            ),
            "docs/deployment/mac_dev_server_pull.md": (
                "bash scripts/server_test.sh",
                "bash scripts/server_check.sh",
            ),
        }
        for relative_path, (code_gate, host_gate) in commands.items():
            document = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertLess(
                document.index(code_gate),
                document.index(host_gate),
                f"{relative_path} must run the code gate before the host gate",
            )

    def test_ncbi_snapshot_install_commands_have_one_operator_owner(self) -> None:
        authority = (
            PROJECT_ROOT / "data" / "reference" / "ncbi" / "README.md"
        ).read_text(encoding="utf-8")
        installer_name = "install_ncbi_reference.py"

        self.assertIn("sole operator authority", authority)
        self.assertIn(installer_name, authority)
        for relative_path in (
            "docs/deployment/migration.md",
            "docs/deployment/mac_dev_server_pull.md",
        ):
            document = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("../../data/reference/ncbi/README.md", document)
            self.assertNotIn(installer_name, document)
