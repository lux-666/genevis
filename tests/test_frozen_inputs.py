"""Focused contracts for immutable control-input freezing."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.run import execute_frozen_session
from app.execution.dependency_snapshot import (
    capture_dependency_snapshot,
    load_dependency_snapshot,
    load_frozen_dependency_resolver,
)
from app.execution.env_dispatch import resolve_method_env
from app.execution.env_resolver import PreflightResult
from app.schemas.state import initial_state
from app.stages.inventory.nodes import register_assets, validate_inputs
from app.storage.artifacts import resolve_artifact_ref
from app.storage.frozen_inputs import (
    FrozenInputConflictError,
    FrozenInputIntegrityError,
    FrozenInputValidationError,
    freeze_session_inputs,
    frozen_execution_lock,
    prepare_session_inputs,
    validate_frozen_inputs,
)
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout


def _stage_requests(
    *stages: str,
    local: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    return [
        {"stage": stage, "allow_local_execution": stage in local}
        for stage in stages
    ]


def _local_stage_requests(*stages: str) -> list[dict[str, object]]:
    return _stage_requests(*stages, local=stages)


class _FrozenExecutionGraph:
    def __init__(self) -> None:
        self.invocations: list[tuple[object, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return {
            "current_stage": "done",
            "supervision_state": "completed",
            "route_trace": [],
        }


class FrozenInputTests(unittest.TestCase):
    def test_inventory_uses_verified_bundle_after_control_file_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
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
        reads = root / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        manifest = root / "manifest_draft.yaml"
        manifest.write_text(
            "session_id: s1\n"
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
            "packages: {}\n",
            encoding="utf-8",
        )
        return SessionPaths(root / "data", "s1"), manifest, run_config, envs

    def test_prepare_is_no_write_and_preserves_file_supplied_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
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
                            "read_inputs": [
                                {"path": "reads.fastq", "platform": "ont"}
                            ]
                        }
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

    def test_prepared_freeze_revalidates_mutation_before_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, _ = self._fixture(Path(tmp))
            prepared = prepare_session_inputs(
                manifest_draft=manifest,
                run_config_draft=run_config,
            )
            prepared.manifest.session_id = "other-session"
            prepared.run_config.requested_stages.append("not-a-stage")  # type: ignore[arg-type]

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "run-config draft failed validation",
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
                            "read_inputs": [
                                {"path": "reads.fastq", "platform": "ont"}
                            ]
                        }
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

    def test_prepare_rejects_local_assembly_without_genome_size_before_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _local_stage_requests("assembly"),
                        "assembly": {
                            "assembly_method": "flye",
                            "flye_parameters": {},
                            "assembly_route": "ont",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FrozenInputValidationError, "genome size"):
                prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_prepare_rejects_explicit_assembly_method_without_read_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, _ = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _local_stage_requests("assembly"),
                        "assembly": {
                            "assembly_method": "hifiasm_ont",
                            "hifiasm_ont_parameters": {"threads": 4},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "assembly.assembly_method is set but no read_type could be inferred",
            ):
                prepare_session_inputs(
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                )

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_freeze_rejects_inferred_local_method_without_read_type(self) -> None:
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
                        "assembly": {"flye_parameters": {}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "local assembly method 'flye'.*no read_type could be inferred",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_freeze_rejects_local_flye_and_nextdenovo_without_reads(self) -> None:
        for method in ("flye", "nextdenovo"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths, manifest, run_config, envs = self._fixture(root)
                manifest.write_text(
                    "session_id: s1\n"
                    "genome_size_estimate: 1000\n"
                    "assets:\n"
                    "  - asset_type: raw_short_dna_reads\n"
                    "    paths: [reads.fastq]\n"
                    "    platform: illumina\n"
                    "    format: fastq\n",
                    encoding="utf-8",
                )
                run_config.write_text(
                    json.dumps(
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "shared_parameters": {"read_type": "ont_raw"},
                            "assembly": {f"{method}_parameters": {}},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    FrozenInputValidationError,
                    "requires registered long-read declarations",
                ):
                    freeze_session_inputs(
                        paths,
                        manifest_draft=manifest,
                        run_config_draft=run_config,
                        config_path=envs,
                    )

                self.assertFalse(paths.data_root.exists())
                self.assertFalse(paths.run_dir.exists())

    def test_freeze_rejects_local_methods_with_wrong_read_family(self) -> None:
        for method in ("flye", "nextdenovo"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths, manifest, run_config, envs = self._fixture(root)
                manifest.write_text(
                    manifest.read_text(encoding="utf-8")
                    .replace("platform: ont", "platform: pacbio_hifi")
                    .replace(
                        "assets:\n",
                        "genome_size_estimate: 1000\nassets:\n",
                    ),
                    encoding="utf-8",
                )
                run_config.write_text(
                    json.dumps(
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "shared_parameters": {"read_type": "ont_raw"},
                            "assembly": {f"{method}_parameters": {}},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    FrozenInputValidationError,
                    "compatible with read_type 'ont_raw'",
                ):
                    freeze_session_inputs(
                        paths,
                        manifest_draft=manifest,
                        run_config_draft=run_config,
                        config_path=envs,
                    )

                self.assertFalse(paths.data_root.exists())
                self.assertFalse(paths.run_dir.exists())

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
                        {
                            "requested_stages": _local_stage_requests("assembly"),
                            "shared_parameters": {"read_type": read_type},
                            "assembly": {"flye_parameters": {}},
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
                    prepared.declarations.read_declarations[0].platform,
                    platform,
                )
                self.assertFalse(paths.data_root.exists())

    def test_freeze_rejects_method_incompatible_shared_read_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "platform: ont",
                    "platform: pacbio_hifi",
                ),
                encoding="utf-8",
            )
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _local_stage_requests("assembly"),
                        "shared_parameters": {"read_type": "ont_raw"},
                        "assembly": {"hifiasm_parameters": {}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FrozenInputValidationError,
                "incompatible with inferred read_type 'ont_raw'",
            ):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

            self.assertFalse(paths.data_root.exists())
            self.assertFalse(paths.run_dir.exists())

    def test_run_config_occurrences_are_the_only_dependency_stage_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": _stage_requests(
                            "assembly", "read_qc", "assembly"
                        )
                    }
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
                json.dumps({"requested_stages": _stage_requests("assembly")}),
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
                json.dumps({"requested_stages": _stage_requests("assembly")}),
                encoding="utf-8",
            )

            with self.assertRaises(FrozenInputValidationError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                    selected_methods={
                        "assembly": ["assembly_review.eval.quast"]
                    },
                )

    def test_freeze_normalizes_explicit_paired_hic_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            hic_dir = root / "hic"
            hic_dir.mkdir()
            (hic_dir / "R1.fastq.gz").write_text("R1\n", encoding="utf-8")
            (hic_dir / "R2.fastq.gz").write_text("R2\n", encoding="utf-8")
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
                json.dumps({"requested_stages": _stage_requests("assembly")}),
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
            with patch(
                "app.execution.env_dispatch.preflight",
                return_value=PreflightResult(
                    status="ok", env_target="original-assembly"
                ),
            ):
                spec, packages, blocked = resolve_method_env(
                    {},
                    stage="assembly",
                    method_name="assembly.unknown",
                    workflow_step_id="step-000-assembly",
                    session_id="s1",
                    data_dir=paths.run_dir,
                    input_manifest_ref="control/input_manifest.yaml",
                    config_path=envs,
                )
            self.assertIsNone(blocked)
            self.assertEqual(spec.conda_env if spec else None, "original-assembly")
            self.assertEqual(packages, [])

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
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch(
                    "app.entrypoints.run.build_graph", return_value=graph
                ) as build_graph,
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
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

            graph = _FrozenExecutionGraph()
            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
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
