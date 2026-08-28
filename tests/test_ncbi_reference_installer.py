"""Contracts for the generic NCBI reference snapshot installer."""

from __future__ import annotations

import hashlib
import json
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
            f"{prefix}.ncbi.tsv": "Organism Name\tOrganism Taxonomic ID\n",
            f"{prefix}.ncbi.lineage": "Organism Taxonomic ID\tLineage\n",
            "species_alias.tsv": "alias\tcanonical_scientific_name\n",
        }
        files = {}
        for name, content in contents.items():
            path = root / name
            path.write_text(content, encoding="utf-8")
            raw = path.read_bytes()
            files[name] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        (root / installer.MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dataset_id": "fixture",
                    "source_provider": "tests",
                    "source_version": "1",
                    "exported_at": "2026-08-26T00:00:00Z",
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
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

    def test_fresh_destination_installs_every_manifest_file(self) -> None:
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
                {*files, installer.MANIFEST_FILENAME},
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
                for name in (*files, installer.MANIFEST_FILENAME):
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
            self.assertEqual(
                (destination / installer.MANIFEST_FILENAME).read_bytes(),
                (source / installer.MANIFEST_FILENAME).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
