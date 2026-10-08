"""Focused contracts for immutable control-input freezing."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import TerminalFinalizationResult
from app.entrypoints.run import execute_frozen_session
from app.execution.dependency_snapshot import (
    DependencySnapshotError,
    FrozenDependencyResolver,
    capture_dependency_snapshot,
    load_dependency_snapshot,
    load_frozen_dependency_resolver,
)
from app.execution.env_dispatch import resolve_method_env
from app.schemas.state import initial_state
from app.stages.inventory.nodes import register_assets, validate_inputs
from app.storage.artifacts import resolve_artifact_ref
from app.storage.frozen_inputs import (
    FrozenExecutionLease,
    FrozenInputConflictError,
    FrozenInputIntegrityError,
    FrozenInputValidationError,
    freeze_session_inputs,
    frozen_execution_lock,
    prepare_session_inputs,
    validate_frozen_inputs,
)
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout
from tests.busco_fixture import ensure_busco_lineage


def _stage_requests(
    *stages: str,
    local: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    return [
        {"stage": stage, "allow_local_execution": stage in local} for stage in stages
    ]


def _local_stage_requests(*stages: str) -> list[dict[str, object]]:
    return _stage_requests(*stages, local=stages)


def _with_busco(root: Path, payload: dict[str, object]) -> dict[str, object]:
    requests = payload.get("requested_stages") or []
    if not any(
        isinstance(request, dict) and request.get("stage") == "assembly_review"
        for request in requests
    ):
        return payload
    lineage = ensure_busco_lineage(root)
    return {
        **payload,
        "assembly_review": {
            "busco": {
                "lineage": lineage.name,
                "threads": 4,
            }
        },
    }


def _review_run_config(
    lineage: Path,
    *,
    lineage_selector: str | None = None,
    **busco_overrides: object,
) -> dict[str, object]:
    selected_lineage = lineage_selector or lineage.name
    return {
        "requested_stages": [
            {"stage": "assembly_review", "allow_local_execution": False}
        ],
        "assembly_review": {
            "busco": {"lineage": selected_lineage, "threads": 4, **busco_overrides}
        },
    }


def _write_database_registry(root: Path) -> Path:
    """Create a complete host-owned DB registry for freeze fixtures."""
    lineage = ensure_busco_lineage(root)
    reference = root / "ncbi_metazoa"
    reference.mkdir(parents=True, exist_ok=True)
    files = {
        "Metazoa.ncbi.json": "{}\n",
        "Metazoa.ncbi.tsv": (
            "Current Accession\tOrganism Name\tOrganism Taxonomic ID\t"
            "Annotation BUSCO Lineage\n"
            "GCF_000000001.1\tFixture species\t1\tembryophyta_odb10\n"
        ),
        "Metazoa.ncbi.lineage": (
            "Organism Taxonomic ID\n1 Eukaryota;Fixture species\n"
        ),
        "Metazoa.ncbi.lineage2": (
            "Organism Taxonomic ID\n1 Eukaryota;Fixture;Fixture species\n"
        ),
    }
    for name, content in files.items():
        (reference / name).write_text(content, encoding="utf-8")
    return lineage


def _finalized_terminal(*args, **_kwargs) -> TerminalFinalizationResult:
    return TerminalFinalizationResult(state=args[2], terminal_event={})


class _FrozenExecutionGraph:
    def __init__(self, *, resumed_state: dict | None = None) -> None:
        self.invocations: list[tuple[object, dict]] = []
        self.resumed_state = resumed_state

    def invoke(self, state, config):
        self.invocations.append((state, config))
        base_state = state if isinstance(state, dict) else self.resumed_state
        if base_state is None:
            raise AssertionError("resumed execution requires a checkpoint state")
        return {
            **base_state,
            "current_stage": "done",
            "supervision_state": "completed",
            "workflow_step_index": len(base_state["workflow_steps"]),
            "entered_workflow_step_index": len(base_state["workflow_steps"]) - 1,
            "route_trace": [],
        }


class FrozenInputTests(unittest.TestCase):
    def test_zero_byte_sequence_input_is_rejected_before_run_layout_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = root / "empty.fastq"
            reads.touch()
            paths = SessionPaths(root / "data", "empty-input")

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "sequence/assembly inputs must not be empty",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft={
                        "session_id": "empty-input",
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(reads.resolve())],
                                "platform": "ont",
                            }
                        ],
                    },
                    run_config_draft={"requested_stages": []},
                )

            self.assertFalse(paths.run_dir.exists())

    def test_logically_empty_compressed_input_is_rejected_before_layout_creation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = root / "empty.fastq.gz"
            reads.write_bytes(gzip.compress(b""))
            paths = SessionPaths(root / "data", "empty-compressed-input")

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "logically empty",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft={
                        "session_id": "empty-compressed-input",
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(reads.resolve())],
                                "platform": "ont",
                            }
                        ],
                    },
                    run_config_draft={"requested_stages": []},
                )

            self.assertFalse(paths.run_dir.exists())

    def test_inventory_uses_verified_bundle_after_control_file_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "shared_parameters": {"platform": "ont"},
                    }
                ),
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            bundle = validate_frozen_inputs(paths)
            paths.run_config_path.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "shared_parameters": {"platform": "pacbio_hifi"},
                    }
                ),
                encoding="utf-8",
            )
            tampered_reads = root / "tampered.fastq"
            tampered_reads.write_text("@tampered\nTGCA\n+\n!!!!\n", encoding="utf-8")
            paths.input_manifest_path.write_text(
                "session_id: s1\n"
                "assets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths: [{tampered_reads}]\n"
                "    platform: pacbio_hifi\n"
                "    format: fastq\n",
                encoding="utf-8",
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref=paths.to_ref(paths.input_manifest_path),
                data_dir=str(paths.run_dir),
                workflow_steps=[
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                        "selected_method_names": [],
                    }
                ],
            )

            with (
                patch(
                    "app.storage.frozen_inputs._load_frozen_manifest",
                    side_effect=AssertionError("Inventory must not reload manifest"),
                ),
                patch(
                    "app.storage.frozen_inputs._load_frozen_run_config",
                    side_effect=AssertionError("Inventory must not reload RunConfig"),
                ),
            ):
                validated = {
                    **state,
                    **validate_inputs(state, verified_inputs=bundle),
                }
                registered = {
                    **validated,
                    **register_assets(validated, verified_inputs=bundle),
                }

            self.assertEqual(validated["shared_parameters"]["platform"], "ont")
            inventory = validated["stage_outputs"]["inventory"]
            self.assertEqual(inventory["asset_count"], 1)
            registry_path = resolve_artifact_ref(
                registered["data_dir"],
                registered["session_id"],
                registered["asset_registry_ref"],
            )
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            asset = registry["assets"][0]
            self.assertEqual(asset["platform"], "ont")
            self.assertEqual(
                asset["provenance"]["manifest_ref"],
                paths.to_ref(paths.input_manifest_path),
            )
            self.assertEqual(
                asset["provenance"]["declarations"][0]["declared_path"],
                str((root / "reads.fastq").resolve()),
            )

    def test_method_env_rejects_unfrozen_implicit_active_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(Path(tmp), "s1")
            ensure_run_layout(paths)

            with self.assertRaisesRegex(
                DataLayoutError,
                "requires a frozen dependency contract",
            ):
                resolve_method_env(
                    {},
                    stage="assembly",
                    method_name="assembly.candidate.flye",
                    workflow_step_id="step-000-assembly",
                    session_id="s1",
                    data_dir=paths.run_dir,
                    input_manifest_ref="control/input_manifest.yaml",
                )

    def _fixture(self, root: Path) -> tuple[SessionPaths, Path, Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        lineage = _write_database_registry(root)
        reads = root / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        manifest = root / "manifest_draft.yaml"
        manifest.write_text(
            "session_id: s1\n"
            "user_metadata:\n"
            "  target_species: Fixture species\n"
            "assets:\n"
            "  - asset_type: raw_long_reads\n"
            "    paths: [reads.fastq]\n"
            "    platform: ont\n"
            "    format: fastq\n",
            encoding="utf-8",
        )
        run_config = root / "run_config_draft.json"
        run_config.write_text('{"requested_stages": []}\n', encoding="utf-8")
        envs = root / "envs.yaml"
        envs.write_text(
            "default:\n  fallback_env: original-env\n"
            "stages:\n  assembly:\n    conda_env: original-assembly\n"
            "packages: {}\n"
            "databases:\n"
            "  busco:\n"
            "    default_lineage: embryophyta_odb10\n"
            "    lineages:\n"
            "      embryophyta_odb10:\n"
            f"        path: {lineage}\n"
            "        version: odb10\n"
            "        source: test_fixture\n"
            "  ncbi:\n"
            f"    reference_dir: {root / 'ncbi_metazoa'}\n",
            encoding="utf-8",
        )
        return SessionPaths(root / "data", "s1"), manifest, run_config, envs

    def test_prepare_is_no_write_and_preserves_file_supplied_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _stage_requests("read_qc"),
                        "shared_parameters": {"platform": "ont"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )

            self.assertEqual(prepared.manifest.session_id, "s1")
            self.assertEqual(
                prepared.manifest.assets[0].paths,
                [str((root / "reads.fastq").resolve())],
            )
            self.assertEqual(
                prepared.run_config.model_fields_set,
                {"requested_stages", "shared_parameters"},
            )
            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_prepare_canonicalizes_relative_run_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, run_config, _ = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "t2t": {
                            "read_inputs": [{"path": "reads.fastq", "platform": "ont"}]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )

            self.assertEqual(
                prepared.run_config.t2t.read_inputs[0].path,
                str((root / "reads.fastq").resolve()),
            )

    def test_prepared_freeze_rejects_manifest_identity_before_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )
            mismatched_paths = SessionPaths(paths.data_root, "s2")

            with self.assertRaisesRegex(FrozenInputValidationError, "session_id"):
                freeze_session_inputs(mismatched_paths, prepared_inputs=prepared)

            self.assertFalse(mismatched_paths.data_root.exists())
            self.assertFalse(mismatched_paths.run_dir.exists())

    def test_prepared_freeze_publishes_the_prepared_canonical_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )

            bundle = freeze_session_inputs(
                paths,
                prepared_inputs=prepared,
                config_path=envs,
            )

            self.assertEqual(bundle.manifest, prepared.manifest)
            self.assertEqual(bundle.run_config, prepared.run_config)

    def test_requested_review_without_busco_uses_species_derived_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {
                                "stage": "assembly_review",
                                "allow_local_execution": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )
            freeze_session_inputs(
                paths,
                prepared_inputs=prepared,
                config_path=envs,
            )

            self.assertTrue(paths.run_dir.exists())

    def test_requested_review_requires_target_species(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _paths, manifest, run_config, _envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "user_metadata:\n  target_species: Fixture species\n", ""
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {
                                "stage": "assembly_review",
                                "allow_local_execution": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "user_metadata.target_species",
            ):
                prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

    def test_explicit_legacy_lineage_does_not_replace_target_species(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _paths, manifest, run_config, _envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "user_metadata:\n  target_species: Fixture species\n", ""
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(_review_run_config(root / "embryophyta_odb10")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "user_metadata.target_species",
            ):
                prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

    def test_requested_review_rejects_conflicting_species_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _paths, manifest, run_config, _envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        **_review_run_config(root / "embryophyta_odb10"),
                        "shared_parameters": {
                            "species_facts": {"scientific_name": "Different species"}
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "scientific_name conflicts",
            ):
                prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

    def test_busco_lineage_must_be_existing_directory_before_freeze(self) -> None:
        for kind in ("missing", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths, manifest, run_config, envs = self._fixture(root)
                lineage = root / "invalid_lineage"
                if kind == "file":
                    lineage.write_text("not a directory\n", encoding="utf-8")
                run_config.write_text(
                    json.dumps(_review_run_config(root / "embryophyta_odb10")),
                    encoding="utf-8",
                )
                envs.write_text(
                    envs.read_text(encoding="utf-8").replace(
                        f"path: {root / 'embryophyta_odb10'}",
                        f"path: {lineage}",
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    FrozenInputValidationError,
                    "does not exist"
                    if kind == "missing"
                    else "must be a directory|is not a directory",
                ):
                    freeze_session_inputs(
                        paths,
                        manifest_draft=manifest,
                        run_config_draft=run_config,
                        config_path=envs,
                    )

                self.assertFalse(paths.data_root.exists())
                self.assertFalse(paths.run_dir.exists())

    def test_busco_lineage_rejects_empty_or_malformed_dataset_before_freeze(
        self,
    ) -> None:
        for kind in (
            "empty_lineage",
            "empty_dataset_config",
            "whitespace_dataset_config",
            "config_directory",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths, manifest, run_config, envs = self._fixture(root)
                lineage = root / "embryophyta_odb10"
                dataset_config = lineage / "dataset.cfg"
                if kind == "empty_lineage":
                    for child in lineage.iterdir():
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                elif kind == "empty_dataset_config":
                    dataset_config.write_text("", encoding="utf-8")
                elif kind == "whitespace_dataset_config":
                    dataset_config.write_text("  \n\t\n", encoding="utf-8")
                elif kind == "config_directory":
                    dataset_config.unlink()
                    dataset_config.mkdir()
                run_config.write_text(
                    json.dumps(_review_run_config(lineage)), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    FrozenInputValidationError,
                    (
                        "dataset.cfg is missing required keys"
                        if kind == "whitespace_dataset_config"
                        else "dataset.cfg must be a non-empty regular file"
                    ),
                ):
                    freeze_session_inputs(
                        paths,
                        manifest_draft=manifest,
                        run_config_draft=run_config,
                        config_path=envs,
                    )

                self.assertFalse(paths.data_root.exists())
                self.assertFalse(paths.run_dir.exists())

    def test_busco_lineage_rejects_incomplete_dataset_body_before_freeze(
        self,
    ) -> None:
        cases = {
            "missing_config_key": "dataset.cfg is missing required keys",
            "missing_scores": "scores_cutoff must be a non-empty regular file",
            "missing_profile": "HMM profile count does not match",
            "malformed_profile": "HMM profile is missing a HMMER3 header",
            "invalid_score": "scores_cutoff contains a non-numeric score",
            "score_profile_mismatch": "profiles must exactly match",
            "missing_lengths": "odb10 lengths_cutoff must be a non-empty",
            "missing_ancestral": "eukaryota ancestral must be a non-empty",
            "missing_refseq": "eukaryota refseq_db.faa.gz must be a non-empty",
            "declared_identity_mismatch": "conflicts with the species-derived lineage",
            "declared_version_mismatch": "lineage_version must match",
        }
        for kind, expected in cases.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths, manifest, run_config, envs = self._fixture(root)
                lineage = ensure_busco_lineage(root)
                lineage_selector: str | None = None
                overrides: dict[str, object] = {}
                if kind == "empty_lineage":
                    for child in lineage.iterdir():
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                elif kind == "missing_config_key":
                    dataset_config = lineage / "dataset.cfg"
                    dataset_config.write_text(
                        dataset_config.read_text(encoding="utf-8").replace(
                            "domain=eukaryota\n", ""
                        ),
                        encoding="utf-8",
                    )
                elif kind == "missing_scores":
                    (lineage / "scores_cutoff").unlink()
                elif kind == "missing_profile":
                    next((lineage / "hmms").glob("*.hmm")).unlink()
                elif kind == "malformed_profile":
                    next((lineage / "hmms").glob("*.hmm")).write_text(
                        "not an HMM profile\n",
                        encoding="utf-8",
                    )
                elif kind == "invalid_score":
                    profile_id = next((lineage / "hmms").glob("*.hmm")).stem
                    (lineage / "scores_cutoff").write_text(
                        f"{profile_id} not-a-number\n",
                        encoding="utf-8",
                    )
                elif kind == "score_profile_mismatch":
                    (lineage / "scores_cutoff").write_text(
                        "different_profile 42.0\n",
                        encoding="utf-8",
                    )
                elif kind == "missing_lengths":
                    (lineage / "lengths_cutoff").unlink()
                elif kind == "missing_ancestral":
                    (lineage / "ancestral").unlink()
                elif kind == "missing_refseq":
                    (lineage / "refseq_db.faa.gz").unlink()
                elif kind == "declared_identity_mismatch":
                    lineage_selector = "insecta_odb10"
                elif kind == "declared_version_mismatch":
                    envs.write_text(
                        envs.read_text(encoding="utf-8").replace(
                            "        version: odb10\n", "        version: odb12\n"
                        ),
                        encoding="utf-8",
                    )
                run_config.write_text(
                    json.dumps(
                        _review_run_config(
                            lineage,
                            lineage_selector=lineage_selector,
                            **overrides,
                        )
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(FrozenInputValidationError, expected):
                    freeze_session_inputs(
                        paths,
                        manifest_draft=manifest,
                        run_config_draft=run_config,
                        config_path=envs,
                    )

                self.assertFalse(paths.data_root.exists())
                self.assertFalse(paths.run_dir.exists())

    def test_busco_lineage_contract_is_versioned_for_supported_datasets(self) -> None:
        for lineage_name, expects_lengths in (
            ("embryophyta_odb10", True),
            ("embryophyta_odb12", False),
            ("embryophyta_odb12.2", True),
        ):
            with (
                self.subTest(lineage_name=lineage_name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                _paths, manifest, run_config, _ = self._fixture(root)
                lineage = ensure_busco_lineage(root, lineage_name=lineage_name)
                self.assertEqual((lineage / "lengths_cutoff").exists(), expects_lengths)
                run_config.write_text(
                    json.dumps(_review_run_config(lineage)),
                    encoding="utf-8",
                )

                prepared = prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

                self.assertEqual(
                    prepared.run_config.assembly_review.busco.lineage,
                    lineage_name,
                )

    def test_prepared_freeze_revalidates_mutation_before_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, _ = self._fixture(Path(tmp))
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )
            prepared.manifest.session_id = "other-session"
            prepared.run_config.requested_stages.append("not-a-stage")  # type: ignore[arg-type]

            with (
                self.assertWarns(UserWarning),
                self.assertRaisesRegex(
                    FrozenInputValidationError, "run-config draft failed validation"
                ),
            ):
                freeze_session_inputs(paths, prepared_inputs=prepared)

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_prepared_freeze_deep_canonicalizes_mutated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "t2t": {
                            "read_inputs": [{"path": "reads.fastq", "platform": "ont"}]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )
            noncanonical = str(root / "nested" / ".." / "reads.fastq")
            prepared.manifest.assets[0].paths[0] = noncanonical
            assert prepared.run_config.t2t is not None
            prepared.run_config.t2t.read_inputs[0].path = noncanonical

            bundle = freeze_session_inputs(
                paths,
                prepared_inputs=prepared,
                config_path=envs,
            )

            canonical = str((root / "reads.fastq").resolve())
            self.assertEqual(bundle.manifest.assets[0].paths, [canonical])
            assert bundle.run_config.t2t is not None
            self.assertEqual(bundle.run_config.t2t.read_inputs[0].path, canonical)
            self.assertEqual(prepared.manifest.assets[0].paths, [noncanonical])
            self.assertEqual(
                prepared.run_config.t2t.read_inputs[0].path,
                noncanonical,
            )

    def test_freeze_requires_exactly_one_input_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, _ = self._fixture(Path(tmp))
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )

            with self.assertRaisesRegex(FrozenInputValidationError, "either"):
                freeze_session_inputs(
                    paths,
                    prepared_inputs=prepared,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )
            with self.assertRaisesRegex(FrozenInputValidationError, "requires"):
                freeze_session_inputs(paths)

            self.assertFalse(paths.data_root.exists())

    def test_prepare_accepts_deferred_local_assembly_without_stage_method_facts(
        self,
    ) -> None:
        """Assembly method and genome/read-role checks belong to stage time."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        root,
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "assembly": {"assembly_route": "auto"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )

            assert prepared.run_config.assembly is not None
            self.assertEqual(prepared.run_config.assembly.assembly_route, "auto")
            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_freeze_rejects_explicit_route_read_type_platform_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "platform: ont", "platform: pacbio_hifi"
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        root,
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "shared_parameters": {"read_type": "ont_raw"},
                            "assembly": {"assembly_route": "ont"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "read_type conflicts with the registered long-read platform",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

    def test_prepare_accepts_registered_read_type_families(self) -> None:
        cases = (
            ("ont_raw", "ont"),
            ("ont_hq", "ont"),
            ("cyclone", "cycloneseq"),
            ("pacbio_clr", "clr"),
            ("pacbio_hifi", "pacbio_hifi"),
        )
        for read_type, platform in cases:
            with (
                self.subTest(read_type=read_type, platform=platform),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                paths, manifest, run_config, _ = self._fixture(root)
                manifest.write_text(
                    manifest.read_text(encoding="utf-8")
                    .replace("platform: ont", f"platform: {platform}")
                    .replace(
                        "assets:\n",
                        "genome_size_estimate: 1000\nassets:\n",
                    ),
                    encoding="utf-8",
                )
                run_config.write_text(
                    json.dumps(
                        _with_busco(
                            root,
                            {
                                "requested_stages": _local_stage_requests("assembly"),
                                "shared_parameters": {"read_type": read_type},
                                "assembly": {},
                            },
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )

                prepared = prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

                self.assertEqual(
                    prepared.declarations.read_declarations[0].platform,
                    platform,
                )
                self.assertFalse(paths.data_root.exists())

    def test_freeze_rejects_auto_route_read_type_platform_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "platform: ont",
                    "platform: pacbio_hifi",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        root,
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "shared_parameters": {"read_type": "ont_raw"},
                            "assembly": {"assembly_route": "auto"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "read_type conflicts with the registered long-read platform",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

    def test_run_config_occurrences_are_the_only_dependency_stage_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        root,
                        {
                            "requested_stages": _stage_requests(
                                "assembly", "read_qc", "assembly"
                            )
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            envs.write_text(
                "default:\n  fallback_env: original-env\n"
                "stages:\n"
                "  assembly:\n    conda_env: original-assembly\n"
                "  read_qc:\n    conda_env: original-read-qc\n"
                "packages: {}\n",
                encoding="utf-8",
            )
            bundle = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_methods={},
            )
            snapshot = load_dependency_snapshot(
                paths.dependency_manifest_path,
                expected_sha256=bundle.marker.files["dependency_manifest"].sha256,
            )
            self.assertEqual(
                bundle.run_config.requested_stage_names(),
                ["assembly", "read_qc", "assembly"],
            )
            self.assertEqual(
                set(snapshot.stage_environments),
                {"assembly", "read_qc"},
            )

    def test_legacy_local_exec_is_rejected_during_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "local_exec": ["read_qc"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FrozenInputValidationError, "local_exec"):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_empty_scope_freezes_no_stage_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps({"requested_stages": []}) + "\n",
                encoding="utf-8",
            )

            bundle = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            snapshot = load_dependency_snapshot(
                paths.dependency_manifest_path,
                expected_sha256=bundle.marker.files["dependency_manifest"].sha256,
            )
            self.assertEqual(snapshot.stage_environments, {})
            self.assertTrue(paths.run_dir.exists())

    def test_freeze_is_create_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))

            first = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            marker_before = paths.frozen_inputs_path.read_bytes()

            self.assertTrue(paths.input_manifest_path.is_file())
            self.assertTrue(paths.run_config_path.is_file())
            self.assertTrue(paths.dependency_manifest_path.is_file())
            self.assertEqual(
                first.manifest.assets[0].paths,
                [str((Path(tmp) / "reads.fastq").resolve())],
            )
            self.assertEqual(
                first.marker.files["input_manifest"].path_ref,
                "control/input_manifest.yaml",
            )

            second = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            self.assertEqual(marker_before, paths.frozen_inputs_path.read_bytes())
            self.assertEqual(second.marker, first.marker)
            self.assertEqual(
                validate_frozen_inputs(paths).run_config,
                second.run_config,
            )

    def test_partial_transaction_and_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )

            paths.input_manifest_path.write_text(
                paths.input_manifest_path.read_text(encoding="utf-8") + "# tampered\n",
                encoding="utf-8",
            )
            with self.assertRaises(FrozenInputIntegrityError):
                validate_frozen_inputs(paths)
            with self.assertRaises(FrozenInputIntegrityError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

            fresh_paths, fresh_manifest, fresh_config, fresh_envs = self._fixture(
                Path(tmp) / "fresh"
            )
            ensure_run_layout(fresh_paths)
            fresh_paths.input_manifest_path.write_text("partial\n", encoding="utf-8")
            with self.assertRaises(FrozenInputIntegrityError):
                freeze_session_inputs(
                    fresh_paths,
                    manifest_draft=fresh_manifest,
                    run_config_draft=fresh_config,
                    config_path=fresh_envs,
                )

    def test_different_draft_cannot_replace_frozen_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            changed = Path(tmp) / "changed.yaml"
            changed.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "reads.fastq", "other.fastq"
                ),
                encoding="utf-8",
            )
            (Path(tmp) / "other.fastq").write_text(
                "@r2\nTGCA\n+\n!!!!\n", encoding="utf-8"
            )
            with self.assertRaises(FrozenInputConflictError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=changed,
                    run_config_draft=run_config,
                    config_path=envs,
                )

    def test_different_dependency_selection_cannot_reuse_frozen_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        Path(tmp),
                        {"requested_stages": _stage_requests("assembly")},
                    )
                ),
                encoding="utf-8",
            )
            envs.write_text(
                envs.read_text(encoding="utf-8")
                + "methods:\n"
                + "  assembly.candidate.flye: {conda_env: flye-env}\n"
                + "  assembly.candidate.hifiasm: {conda_env: hifiasm-env}\n",
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_methods={"assembly": ["assembly.candidate.flye"]},
            )

            with self.assertRaises(FrozenInputConflictError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                    selected_methods={"assembly": ["assembly.candidate.hifiasm"]},
                )

    def test_selected_method_must_belong_to_its_declared_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        Path(tmp),
                        {"requested_stages": _stage_requests("assembly")},
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaises(FrozenInputValidationError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                    selected_methods={"assembly": ["assembly_review.eval.quast"]},
                )

    def test_freeze_normalizes_explicit_paired_hic_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            hic_dir = root / "hic"
            hic_dir.mkdir()
            (hic_dir / "R1.fastq.gz").write_bytes(gzip.compress(b"R1\n"))
            (hic_dir / "R2.fastq.gz").write_bytes(gzip.compress(b"R2\n"))
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                + "  - asset_type: hic_reads_registered\n"
                + "    platform: illumina\n"
                + "    layout: paired\n"
                + "    format: fastq\n"
                + "    paired_reads:\n"
                + "      - r1: hic/R1.fastq.gz\n"
                + "        r2: hic/R2.fastq.gz\n",
                encoding="utf-8",
            )

            bundle = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )

            hic_asset = next(
                asset
                for asset in bundle.manifest.assets
                if asset.asset_type == "hic_reads_registered"
            )
            expected = [
                str((hic_dir / "R1.fastq.gz").resolve()),
                str((hic_dir / "R2.fastq.gz").resolve()),
            ]
            self.assertEqual(hic_asset.paths, expected)
            self.assertEqual(hic_asset.paired_reads[0].r1, expected[0])
            self.assertEqual(hic_asset.paired_reads[0].r2, expected[1])

    def test_dependency_resolver_uses_snapshot_after_live_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            run_config.write_text(
                json.dumps(
                    _with_busco(
                        Path(tmp),
                        {"requested_stages": _stage_requests("assembly")},
                    )
                ),
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_methods={},
            )
            envs.write_text(
                "default:\n  fallback_env: changed-live-env\n"
                "stages:\n  assembly:\n    conda_env: changed-live-assembly\n"
                "packages: {}\n",
                encoding="utf-8",
            )

            resolver = load_frozen_dependency_resolver(paths.run_dir)
            self.assertEqual(
                resolver.resolve(stage="assembly").conda_env, "original-assembly"
            )
            with self.assertRaisesRegex(
                DependencySnapshotError,
                "requested method is absent from the frozen dependency snapshot",
            ):
                resolve_method_env(
                    {},
                    stage="assembly",
                    method_name="assembly.unknown",
                    workflow_step_id="step-000-assembly",
                    session_id="s1",
                    data_dir=paths.run_dir,
                    input_manifest_ref="control/input_manifest.yaml",
                    config_path=envs,
                )

    def test_execute_frozen_session_uses_verified_canonical_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            run_config.write_text(
                json.dumps(
                    {"requested_stages": _stage_requests("read_qc", local=("read_qc",))}
                )
                + "\n",
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            envs.unlink()

            graph = _FrozenExecutionGraph()
            captured: dict = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return initial_state(**kwargs)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch(
                    "app.entrypoints.run.build_graph", return_value=graph
                ) as build_graph,
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ),
                patch("app.entrypoints.run.project_workflow_state"),
                patch(
                    "app.execution.dependency_snapshot.EnvResolver",
                    side_effect=AssertionError("live config must not be loaded"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="memory",
                    interactive_gate=False,
                )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            verified_bundle = build_graph.call_args.kwargs["verified_inputs"]
            self.assertEqual(verified_bundle.paths, paths)
            self.assertEqual(
                verified_bundle.declarations.manifest,
                verified_bundle.manifest,
            )
            self.assertEqual(
                captured["input_manifest_ref"],
                "control/input_manifest.yaml",
            )
            self.assertEqual(captured["data_dir"], str(paths.run_dir))
            self.assertEqual(
                [
                    (step["stage"], step["allow_local_execution"])
                    for step in captured["workflow_steps"]
                ],
                [("inventory", False), ("read_qc", True)],
            )
            self.assertNotIn(
                "execution_mode",
                captured["stage_seeds"].get("read_qc", {}),
            )

    def test_execution_lock_refuses_duplicate_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            stderr = io.StringIO()

            with (
                frozen_execution_lock(paths),
                patch("app.entrypoints.run.build_graph") as build_graph,
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 1)
            self.assertIn("already active", stderr.getvalue())
            build_graph.assert_not_called()

    def test_execution_lock_yields_one_active_session_bound_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )

            with frozen_execution_lock(paths) as lease:
                self.assertIsInstance(lease, FrozenExecutionLease)
                lease.require_active(paths)
                self.assertTrue(lease.active)
                self.assertEqual(lease.session_id, paths.session_id)
                self.assertEqual(lease.run_dir, paths.run_dir)

            self.assertFalse(lease.active)
            with self.assertRaisesRegex(ValueError, "inactive"):
                lease.require_active(paths)

    def test_execution_lease_rejects_a_different_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            other = SessionPaths(paths.data_root, "s2")

            with frozen_execution_lock(paths) as lease:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    lease.require_active(other)

    def test_checkpoint_execution_continues_without_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            paths.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(paths.checkpoint_db_path)
            try:
                connection.execute(
                    "CREATE TABLE checkpoints ("
                    "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, "
                    "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, "
                    "type TEXT, checkpoint BLOB, metadata BLOB, "
                    "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
                )
                connection.execute(
                    "INSERT INTO checkpoints VALUES (?, '', 'cp1', NULL, "
                    "'msgpack', ?, ?)",
                    ("s1", b"checkpoint", b"metadata"),
                )
                connection.commit()
            finally:
                connection.close()

            graph = _FrozenExecutionGraph(
                resumed_state=initial_state(
                    session_id="s1",
                    input_manifest_ref="control/input_manifest.yaml",
                    data_dir=str(paths.run_dir),
                )
            )
            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.finalize_authoritative_terminal",
                    side_effect=_finalized_terminal,
                ),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 0)
            self.assertIsNone(graph.invocations[0][0])

    def test_partial_execution_without_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            paths.ledger_events_path.parent.mkdir(parents=True, exist_ok=True)
            paths.ledger_events_path.write_text("partial\n", encoding="utf-8")
            stderr = io.StringIO()

            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 1)
            self.assertIn("without a resumable checkpoint", stderr.getvalue())
            build_graph.assert_not_called()

    def test_dependency_snapshot_uses_selected_execution_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected_root = root / "selected-package"
            internal_root = root / "internal-package"
            unrelated_root = root / "unrelated-package"
            for package_root in (selected_root, internal_root, unrelated_root):
                package_root.mkdir()
                (package_root / "canary").write_text("ok\n", encoding="utf-8")
            envs = root / "envs.yaml"
            envs.write_text(
                "default:\n  fallback_env: default-env\n"
                "stages:\n"
                "  assembly: {conda_env: assembly-env}\n"
                "  assembly_dedup: {conda_env: dedup-env}\n"
                "  assembly_review: {conda_env: review-env}\n"
                "methods:\n"
                "  assembly.candidate.flye:\n"
                "    conda_env: flye-env\n"
                "    package_tools: [selected]\n"
                "  assembly.candidate.hifiasm:\n"
                "    conda_env: hifiasm-env\n"
                "    package_tools: [unrelated]\n"
                "  assembly_dedup.purge_dups:\n"
                "    conda_env: dedup-method-env\n"
                "  assembly_review.eval.quast:\n"
                "    conda_env: quast-env\n"
                "    package_tools: [internal]\n"
                "packages:\n"
                "  selected:\n"
                f"    install_root: {selected_root}\n"
                "    canary_paths: [canary]\n"
                "  internal:\n"
                f"    install_root: {internal_root}\n"
                "    canary_paths: [canary]\n"
                "  unrelated:\n"
                f"    install_root: {unrelated_root}\n"
                "    canary_paths: [canary]\n",
                encoding="utf-8",
            )

            snapshot = capture_dependency_snapshot(
                config_path=envs,
                stages=["assembly", "assembly_dedup", "assembly_review"],
                methods=[
                    "assembly.candidate.flye",
                    "assembly_review.eval.quast",
                ],
            )

            self.assertEqual(
                set(snapshot.stage_environments),
                {"assembly", "assembly_dedup", "assembly_review"},
            )
            self.assertEqual(
                set(snapshot.method_environments),
                {
                    "assembly.candidate.flye",
                    "assembly_dedup.purge_dups",
                    "assembly_review.eval.quast",
                },
            )
            self.assertEqual(
                snapshot.selected_methods,
                ["assembly.candidate.flye", "assembly_review.eval.quast"],
            )
            self.assertEqual(set(snapshot.packages), {"selected", "internal"})

    def test_deferred_assembly_freeze_captures_all_candidate_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "assets:\n",
                    "genome_size_estimate: 1000\nassets:\n",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _local_stage_requests("assembly"),
                        "shared_parameters": {"platform": "ont"},
                        "assembly": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            envs.write_text(
                "default:\n  fallback_env: default-env\n"
                "stages:\n  assembly: {conda_env: assembly-env}\n"
                "packages: {}\n"
                "methods:\n"
                "  assembly.candidate.flye: {conda_env: flye-env, "
                "required_tools: [flye]}\n"
                "  assembly.candidate.nextdenovo: {conda_env: nextdenovo-env, "
                "required_tools: [nextdenovo]}\n"
                "  assembly.candidate.hifiasm: {conda_env: hifiasm-env, "
                "required_tools: [hifiasm]}\n"
                "  assembly.candidate.hifiasm_ont: {conda_env: hifiasm-ont-env, "
                "required_tools: [hifiasm]}\n",
                encoding="utf-8",
            )

            bundle = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            snapshot = load_dependency_snapshot(
                paths.dependency_manifest_path,
                expected_sha256=bundle.marker.files["dependency_manifest"].sha256,
            )

            expected = {
                "assembly.candidate.flye",
                "assembly.candidate.nextdenovo",
                "assembly.candidate.hifiasm",
                "assembly.candidate.hifiasm_ont",
            }
            self.assertEqual(set(snapshot.selected_methods), expected)
            self.assertEqual(set(snapshot.method_environments), expected)

    def test_database_registry_is_captured_and_resolved_from_frozen_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envs = Path(tmp) / "envs.yaml"
            envs.write_text(
                "default:\n  fallback_env: default-env\n"
                "databases:\n"
                "  busco:\n"
                "    default_lineage: metazoa_odb10\n"
                "    lineages:\n"
                "      metazoa_odb10:\n"
                "        path: /db/busco/metazoa_odb10\n"
                "        version: odb10\n"
                "        source: busco_downloads\n"
                "      embryophyta_odb10:\n"
                "        path: /db/busco/embryophyta_odb10\n"
                "        version: odb10\n"
                "        source: busco_downloads\n"
                "  ncbi:\n"
                "    reference_dir: /db/ncbi_metazoa\n",
                encoding="utf-8",
            )

            snapshot = capture_dependency_snapshot(config_path=envs)
            self.assertIsNotNone(snapshot.databases)
            assert snapshot.databases is not None
            self.assertEqual(snapshot.databases.busco.default_lineage, "metazoa_odb10")
            frozen = FrozenDependencyResolver(snapshot)
            self.assertEqual(
                frozen.resolve_busco_lineage("embryophyta_odb10").path,
                "/db/busco/embryophyta_odb10",
            )
            self.assertEqual(
                frozen.resolve_busco_lineage().path, "/db/busco/metazoa_odb10"
            )

    def test_pre_database_v2_snapshot_without_registry_remains_loadable(self) -> None:
        """Adding the optional registry must not break existing v2 manifests."""
        with tempfile.TemporaryDirectory() as tmp:
            envs = Path(tmp) / "envs.yaml"
            envs.write_text("default:\n  fallback_env: default-env\n", encoding="utf-8")
            current = capture_dependency_snapshot(config_path=envs)
            legacy_payload = current.model_dump(mode="json", exclude={"databases"})
            manifest = Path(tmp) / "dependency_manifest.json"
            manifest.write_text(
                json.dumps(legacy_payload, sort_keys=True) + "\n", encoding="utf-8"
            )

            loaded = load_dependency_snapshot(manifest)

            self.assertEqual(loaded.schema_version, 2)
            self.assertIsNone(loaded.databases)

    def test_execute_frozen_session_rejects_any_tampered_control_input(self) -> None:
        targets = (
            "input_manifest_path",
            "run_config_path",
            "dependency_manifest_path",
        )
        for target_name in targets:
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                paths, manifest, run_config, envs = self._fixture(Path(tmp))
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )
                target = getattr(paths, target_name)
                target.write_bytes(target.read_bytes() + b"\n")
                stderr = io.StringIO()

                with (
                    patch("app.entrypoints.run.build_graph") as build_graph,
                    contextlib.redirect_stderr(stderr),
                ):
                    code = execute_frozen_session(
                        "s1",
                        paths.data_root,
                        checkpointer="memory",
                        interactive_gate=False,
                    )

                self.assertEqual(code, 1)
                self.assertIn("frozen session cannot execute", stderr.getvalue())
                detail = stderr.getvalue()
                self.assertTrue(
                    "hash mismatch" in detail or "does not match marker" in detail,
                    detail,
                )
                build_graph.assert_not_called()
