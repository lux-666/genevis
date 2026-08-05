"""Contract tests for user-facing and HPC-generated configuration examples."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from app.schemas.manifest import InputManifest
from app.schemas.run_config import (
    RunConfig,
    load_run_config,
    validate_genome_size_execution_plan,
)
from app.workflow import STAGE_ORDER

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "docs" / "examples"
HPC_COMMON = PROJECT_ROOT / "tests" / "hpc" / "common.sh"


class HPCRunConfigWriterContractTests(unittest.TestCase):
    def test_assembly_configs_keep_genome_size_in_manifest_for_every_assembler(
        self,
    ):
        cases = (
            ("flye", "ont", "ont_raw", "flye_parameters"),
            ("nextdenovo", "ont", "ont_raw", "nextdenovo_parameters"),
            ("hifiasm", "pacbio_hifi", "pacbio_hifi", "hifiasm_parameters"),
            ("hifiasm_ont", "ont", "ont_raw", "hifiasm_ont_parameters"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = root / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")

            for method, platform, read_type, parameter_key in cases:
                with self.subTest(method=method):
                    manifest_path = root / f"{method}.manifest.yaml"
                    run_config_path = root / f"{method}.run_config.json"
                    environment = {
                        **os.environ,
                        "GENE_AGENT_HPC_SKIP_CONFIG": "true",
                        "SAMPLE_ID": "contract-sample",
                        "SPECIES": "contract-species",
                        "GENOME_SIZE_ESTIMATE": "2000000",
                        "PLATFORM": platform,
                        "ASSEMBLY_METHOD": method,
                        "ASSEMBLY_THREADS": "4",
                        "BUSCO_LINEAGE_PATH": "/tmp/contract-busco",
                        "BUSCO_LINEAGE_NAME": "contract_odb10",
                        "HIC_RESTRICTION_ENZYME": "GATC",
                    }
                    subprocess.run(
                        [
                            "bash",
                            "-c",
                            (
                                'source "$1"\n'
                                'write_manifest "$2" contract-session '
                                '"raw_long_reads:$4:$PLATFORM:single"\n'
                                'write_run_config_stages "$3" assembly\n'
                            ),
                            "bash",
                            str(HPC_COMMON),
                            str(manifest_path),
                            str(run_config_path),
                            str(reads),
                        ],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                    manifest = InputManifest.model_validate(
                        yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                    )
                    config = load_run_config(run_config_path)
                    payload = json.loads(run_config_path.read_text(encoding="utf-8"))

                    self.assertEqual(manifest.genome_size_estimate, 2_000_000)
                    self.assertEqual(
                        payload["shared_parameters"],
                        {"read_type": read_type},
                    )
                    self.assertEqual(
                        payload["assembly"][parameter_key],
                        {"threads": 4},
                    )
                    self.assertNotIn(
                        "estimated_genome_size",
                        run_config_path.read_text(encoding="utf-8"),
                    )
                    validate_genome_size_execution_plan(config, manifest)


class PublishedConfigurationContractTests(unittest.TestCase):
    def test_all_run_config_templates_load_with_current_schema(self):
        templates = sorted(EXAMPLES_DIR.glob("*run_config.template.json"))
        self.assertTrue(templates)

        canonical_positions = {
            stage: position
            for position, stage in enumerate(STAGE_ORDER)
            if stage != "inventory"
        }

        for path in templates:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("requested_stages", payload)
                self.assertIn("local_exec", payload)

                config = load_run_config(path)
                self.assertIn("requested_stages", config.model_fields_set)
                self.assertIn("local_exec", config.model_fields_set)

                requested = list(config.requested_stages or [])
                local_exec = list(config.local_exec)
                self.assertTrue(requested)
                self.assertEqual(len(requested), len(set(requested)))
                self.assertEqual(len(local_exec), len(set(local_exec)))
                self.assertTrue(set(local_exec).issubset(requested))
                self.assertEqual(
                    requested,
                    sorted(requested, key=canonical_positions.__getitem__),
                )
                self.assertEqual(
                    local_exec,
                    sorted(local_exec, key=canonical_positions.__getitem__),
                )

    def test_assembly_templates_pair_with_manifest_owned_genome_size(self):
        pairs = (
            (
                "real_data_manifest.template.yaml",
                "read_qc_assembly_review_run_config.template.json",
            ),
            (
                "e2e_manifest.template.yaml",
                "e2e_run_config.template.json",
            ),
        )

        for manifest_name, run_config_name in pairs:
            with self.subTest(run_config=run_config_name):
                manifest = InputManifest.model_validate(
                    yaml.safe_load(
                        (EXAMPLES_DIR / manifest_name).read_text(encoding="utf-8")
                    )
                )
                config = load_run_config(EXAMPLES_DIR / run_config_name)

                self.assertIsNotNone(manifest.genome_size_estimate)
                validate_genome_size_execution_plan(config, manifest)

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
        self.assertIn("local_exec", config.model_fields_set)
        assert config.requested_stages is not None
        canonical_positions = {
            stage: position
            for position, stage in enumerate(STAGE_ORDER)
            if stage != "inventory"
        }
        self.assertEqual(
            config.requested_stages,
            sorted(config.requested_stages, key=canonical_positions.__getitem__),
        )
        self.assertEqual(
            config.local_exec,
            sorted(config.local_exec, key=canonical_positions.__getitem__),
        )
        self.assertTrue(set(config.local_exec).issubset(config.requested_stages))

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
