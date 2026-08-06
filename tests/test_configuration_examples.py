"""Contract tests for published user-facing configuration examples."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

from app.schemas.manifest import InputManifest
from app.schemas.run_config import (
    RunConfig,
    load_run_config,
    validate_genome_size_execution_plan,
)

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

    def test_assembly_template_pairs_with_manifest_owned_genome_size(self):
        pairs = (
            (
                "real_data_manifest.template.yaml",
                "read_qc_assembly_review_run_config.template.json",
                True,
                ["read_qc", "assembly", "assembly_review"],
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
                if needs_genome_size:
                    self.assertIsNotNone(manifest.genome_size_estimate)
                    validate_genome_size_execution_plan(config, manifest)
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
            ],
        )

    def test_readme_run_config_example_loads_with_current_schema(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("### 2. RunConfig Layer", maxsplit=1)[1].split(
            "### 3. Stage selection and gates",
            maxsplit=1,
        )[0]
        match = re.search(r"```json\n(?P<payload>.*?)\n```", section, flags=re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None

        config = RunConfig.model_validate(json.loads(match.group("payload")))

        self.assertIn("requested_stages", config.model_fields_set)
        self.assertEqual(
            [request.model_dump(mode="json") for request in config.requested_stages],
            json.loads(match.group("payload"))["requested_stages"],
        )

        assert config.shared_parameters is not None
        self.assertEqual(config.shared_parameters.read_type, "ont_raw")
        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(
            config.assembly.flye_parameters.model_dump(exclude_none=True),
            {"threads": 16},
        )


if __name__ == "__main__":
    unittest.main()
