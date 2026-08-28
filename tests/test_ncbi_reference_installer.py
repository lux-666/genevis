"""Contracts for the generic NCBI reference snapshot installer."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from scripts import install_ncbi_reference as installer


class NcbiReferenceInstallerTests(unittest.TestCase):
    def _snapshot(self, root: Path, prefix: str) -> tuple[str, ...]:
        root.mkdir(parents=True)
        contents = {
            f"{prefix}.ncbi.json": '{"assembly": 1}\n{"assembly": 2}\n',
            f"{prefix}.ncbi.tsv": "Organism Name\tOrganism Taxonomic ID\n",
            f"{prefix}.ncbi.lineage": "Organism Taxonomic ID\n",
            f"{prefix}.ncbi.lineage2": "Organism Taxonomic ID\n",
        }
        for name, content in contents.items():
            (root / name).write_text(content, encoding="utf-8")
        return tuple(contents)

    def _config(self, root: Path, destination: Path) -> Path:
        config = root / "config" / "envs.yaml"
        config.parent.mkdir()
        config.write_text(
            yaml.safe_dump(
                {
                    "default": {"fallback_env": "tools"},
                    "databases": {
                        "busco": {
                            "default_lineage": "metazoa_odb10",
                            "lineages": {
                                "metazoa_odb10": {
                                    "path": str(root / "busco"),
                                    "version": "odb10",
                                    "source": "tests",
                                }
                            },
                        },
                        "ncbi": {"reference_dir": str(destination)},
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return config

    def test_fresh_destination_installs_all_four_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination_root = root / "installed"
            destination = destination_root / "metazoa"
            files = self._snapshot(source, "Metazoa")
            config = self._config(root, destination_root)

            code = installer.main(
                [
                    "--reference",
                    "metazoa",
                    "--config",
                    str(config),
                    "--source-dir",
                    str(source),
                ]
            )

            self.assertEqual(code, 0)
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                set(files),
            )

    def test_source_url_installs_one_complete_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination_root = root / "installed"
            destination = destination_root / "metazoa"
            archive = root / "snapshot.zip"
            files = self._snapshot(source, "Metazoa")
            config = self._config(root, destination_root)
            with zipfile.ZipFile(archive, "w") as bundle:
                for name in files:
                    bundle.write(source / name, arcname=name)

            code = installer.main(
                [
                    "--reference",
                    "metazoa",
                    "--config",
                    str(config),
                    "--source-url",
                    archive.as_uri(),
                ]
            )

            self.assertEqual(code, 0)
            self.assertEqual({path.name for path in destination.iterdir()}, set(files))


if __name__ == "__main__":
    unittest.main()
