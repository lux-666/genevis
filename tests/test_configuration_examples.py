"""Contract tests for published user-facing configuration examples."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from app.schemas.manifest import InputManifest
from app.schemas.run_config import load_run_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "docs" / "examples"


class PublishedConfigurationContractTests(unittest.TestCase):
    def test_all_run_config_templates_load_with_current_schema(self):
        templates = sorted(EXAMPLES_DIR.glob("*run_config.template.json"))
        self.assertTrue(templates)

        for path in templates:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("requested_stages", payload)
                self.assertNotIn("local_exec", payload)

                config = load_run_config(path)
                self.assertIn("requested_stages", config.model_fields_set)

                requested = config.requested_stage_names()
                self.assertTrue(requested)
                self.assertEqual(
                    [
                        request.model_dump(mode="json")
                        for request in config.requested_stages
                    ],
                    payload["requested_stages"],
                )

    def test_authoritative_contig_template_pairs_with_manifest(self):
        pairs = (
            (
                "contig_manifest.template.yaml",
                "contig_run_config.template.json",
                True,
                [
                    "read_qc",
                    "genome_survey",
                    "assembly",
                    "assembly_dedup",
                    "assembly_review",
                ],
            ),
            (
                "existing_assembly_manifest.template.yaml",
                "assembly_review_run_config.template.json",
                False,
                ["assembly_review"],
            ),
        )

        for manifest_name, run_config_name, needs_genome_size, expected_stages in pairs:
            with self.subTest(run_config=run_config_name):
                manifest = InputManifest.model_validate(
                    yaml.safe_load(
                        (EXAMPLES_DIR / manifest_name).read_text(encoding="utf-8")
                    )
                )
                config = load_run_config(EXAMPLES_DIR / run_config_name)

                self.assertEqual(config.requested_stage_names(), expected_stages)
                self.assertIn("target_species", manifest.user_metadata)
                self.assertNotIn(
                    "lineage",
                    json.loads(
                        (EXAMPLES_DIR / run_config_name).read_text(encoding="utf-8")
                    )
                    .get("assembly_review", {})
                    .get("busco", {}),
                )
                if needs_genome_size:
                    self.assertIsNotNone(manifest.genome_size_estimate)
                    self.assertIsNotNone(config.genome_survey)
                    assert config.genome_survey is not None
                    self.assertIsNotNone(config.genome_survey.survey_parameters)
                    assert config.assembly_dedup is not None
                    self.assertTrue(config.assembly_dedup.automatic_purge_approved)
                else:
                    self.assertEqual(
                        [asset.asset_type for asset in manifest.assets],
                        ["assembly_fasta_registered"],
                    )
                    self.assertTrue(
                        any(
                            request.stage == "assembly_review"
                            and request.allow_local_execution
                            for request in config.requested_stages
                        )
                    )

    def test_repeated_stage_template_preserves_literal_occurrence_sequence(self):
        config = load_run_config(
            EXAMPLES_DIR / "repeated_stage_occurrences_run_config.template.json"
        )

        self.assertEqual(
            [
                (request.stage, request.allow_local_execution)
                for request in config.requested_stages
            ],
            [
                ("t2t", False),
                ("assembly_review", True),
                ("t2t", True),
                ("assembly_review", True),
            ],
        )

    def test_contig_template_defers_method_and_sets_shared_execution_inputs(self):
        path = EXAMPLES_DIR / "contig_run_config.template.json"
        config = load_run_config(path)
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert config.assembly is not None
        assert config.shared_parameters is not None
        assert config.shared_parameters.species_facts is not None
        self.assertEqual(config.assembly.threads, 8)
        self.assertEqual(config.shared_parameters.species_facts.ploidy, 2)
        self.assertNotIn(
            "scientific_name", payload["shared_parameters"]["species_facts"]
        )
        self.assertEqual(
            config.review_policy.effective_max_blocked_seconds("assembly"), 300
        )
        self.assertEqual(
            config.review_policy.effective_max_blocked_seconds("assembly_review"),
            300,
        )

    def test_database_paths_live_only_in_the_host_seed(self):
        forbidden = {
            "lineage_path",
            "lineage_name",
            "lineage_version",
            "lineage_source",
            "related_species_reference_dir",
        }
        for path in sorted(EXAMPLES_DIR.glob("*run_config.template.json")):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(all(field not in text for field in forbidden))

        host_seed = yaml.safe_load(
            (EXAMPLES_DIR / "envs.yaml.example").read_text(encoding="utf-8")
        )
        self.assertIn("busco", host_seed["databases"])
        self.assertIn("ncbi_metazoa", host_seed["databases"])
        self.assertIn(
            "gzip",
            host_seed["methods"]["genome_survey.kmer_count.jellyfish"][
                "required_tools"
            ],
        )

    def test_readme_points_to_authoritative_contig_template(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/examples/contig_manifest.template.yaml", readme)
        self.assertIn("docs/examples/contig_run_config.template.json", readme)

    def test_server_check_derives_requirements_from_authoritative_template(self):
        server_check = (PROJECT_ROOT / "scripts" / "server_check.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs/examples/contig_run_config.template.json", server_check)
        self.assertIn("effective_method_closure", server_check)
        self.assertNotIn("EXEC_TOOLS=", server_check)


if __name__ == "__main__":
    unittest.main()
