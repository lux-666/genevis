"""Small contracts preventing documentation and server-helper drift."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    return [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").rglob("*.md"))]


class DocumentationContractTests(unittest.TestCase):
    def test_assembly_case_contract_names_compact_live_boundary(self) -> None:
        contract = (
            PROJECT_ROOT / "docs/architecture/reference/casepack.md"
        ).read_text()
        for required in (
            "Live directory, model projection and ownership",
            "duplicate representations",
            "CaseContent",
            "intentional symmetry",
            "test_assembly_case_consumption.py",
            "project_method_choice_case()",
            "project_parameter_case()",
            "check_assembly_projection.sh",
            "_recover_assembly_method_artifact()",
            "parameter_prompt_bundle_name()",
        ):
            self.assertIn(required, contract)

    def test_markdown_relative_links_resolve(self) -> None:
        broken: list[str] = []
        for source in _markdown_files():
            lines = source.read_text(encoding="utf-8").splitlines()
            for line_number, line in enumerate(lines, 1):
                for raw_target in MARKDOWN_LINK.findall(line):
                    target = raw_target.strip().strip("<>")
                    if target.startswith(("http://", "https://", "mailto:", "#")):
                        continue
                    relative = target.split("#", 1)[0].split("?", 1)[0]
                    if not relative:
                        continue
                    resolved = (source.parent / relative).resolve()
                    if not resolved.exists():
                        location = source.relative_to(PROJECT_ROOT)
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

        self.assertIn(
            "single owner of repository-local engineering procedure", governance
        )
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
        self.assertIn("navigation layer", developer_index)
        self.assertIn("index only", user_index)

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
        self.assertIn("ignored, machine-local state", bootstrap_topic)
        self.assertIn("never falls back to the example", bootstrap_topic)
        self.assertIn("must remain ignored/untracked", deployment_map)

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
        self.assertIn("does not\nsilently use the example", migration)

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

        self.assertIn("accepts only the exact", execution)
        self.assertIn('parsed JSON object `{"ready": true}`', execution)
        self.assertNotIn('{"ready": true}', resources)
        self.assertIn("ReviewPolicyConfig.max_blocked_seconds", run_data)
        self.assertNotIn("max_blocked_seconds", parameters)
        self.assertNotIn("max_blocked_seconds", stages)
        self.assertIn("Manifest-owned target species", shared)
        self.assertNotIn("Target species has two frozen representations", conformance)
        self.assertIn(
            "Audit finding identifiers are review-local coordinates", governance
        )

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
        self.assertIn("pytest -q", server_test)
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
        self.assertIn("every method entry declared", deployment_map)
        self.assertIn("fixed-stage method/package closure", deployment_map)
        self.assertIn("Assembly method selection remains deferred", deployment_map)
        self.assertIn("not a proof that `server_check.sh` will pass", deployment_map)

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
