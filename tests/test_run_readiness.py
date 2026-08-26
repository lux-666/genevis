"""Confirmed-run readiness checks before the create-only freeze boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.execution.run_readiness import (
    RunReadinessError,
    assembly_candidate_method_scope,
    effective_method_closure,
    preflight_run_readiness,
    probe_llm_endpoint,
    run_requires_llm,
)
from app.schemas.run_config import RunConfig
from tests.busco_fixture import ensure_busco_lineage


def test_llm_probe_requires_the_structured_response_contract() -> None:
    model = SimpleNamespace(
        invoke=lambda _messages: SimpleNamespace(content='{"ready":true}')
    )
    with patch("app.llm.factory.build_chat_model", return_value=model):
        probe_llm_endpoint()


def test_llm_probe_rejects_non_json_connectivity_text() -> None:
    model = SimpleNamespace(invoke=lambda _messages: SimpleNamespace(content="READY"))
    with patch("app.llm.factory.build_chat_model", return_value=model):
        with pytest.raises(ValueError):
            probe_llm_endpoint()


def _write_ncbi_reference(root: Path) -> Path:
    reference = root / "ncbi_metazoa"
    reference.mkdir(parents=True, exist_ok=True)
    files = {
        "Metazoa.ncbi.tsv": "accession\n",
        "Metazoa.ncbi.lineage": "taxid\tlineage\n",
        "species_alias.tsv": "alias\tscientific_name\n",
    }
    for name, content in files.items():
        (reference / name).write_text(content, encoding="utf-8")
    manifest_files = {
        name: {
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "size_bytes": len(content.encode()),
        }
        for name, content in files.items()
    }
    (reference / "reference_manifest.json").write_text(
        json.dumps({"files": manifest_files}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return reference


def _assembly_config(
    *,
    review_local: bool = True,
    lineage_version: str = "odb10",
) -> RunConfig:
    lineage_name = f"embryophyta_{lineage_version}"
    return RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly", "allow_local_execution": True},
                {"stage": "assembly_dedup", "allow_local_execution": True},
                {
                    "stage": "assembly_review",
                    "allow_local_execution": review_local,
                },
            ],
            "assembly_review": {
                "busco": {
                    "lineage": lineage_name,
                    "threads": 4,
                }
            },
        }
    )


def _env_config(path: Path) -> Path:
    busco_root = path.parent / "busco"
    lineage_names = (
        "embryophyta_odb10",
        "embryophyta_odb12",
        "embryophyta_odb12.2",
    )
    for lineage_name in lineage_names:
        ensure_busco_lineage(busco_root, lineage_name=lineage_name)
    reference = _write_ncbi_reference(path.parent)
    lineage_yaml = "".join(
        f"      {name}:\n"
        f"        path: {busco_root / name}\n"
        f"        version: {name.removeprefix('embryophyta_')}\n"
        "        source: test_fixture\n"
        for name in lineage_names
    )
    path.write_text(
        "default:\n"
        "  conda_bin: /usr/bin/true\n"
        "  fallback_env: test-env\n"
        "stages: {}\n"
        "packages: {}\n"
        "databases:\n"
        "  busco:\n"
        "    default_lineage: embryophyta_odb10\n"
        "    lineages:\n" + lineage_yaml + "  ncbi_metazoa:\n"
        f"    reference_dir: {reference}\n"
        "methods:\n"
        "  assembly.candidate.flye:\n"
        "    conda_env: test-env\n"
        "    required_tools: [flye]\n"
        "  assembly.candidate.hifiasm:\n"
        "    conda_env: test-env\n"
        "    required_tools: [hifiasm]\n"
        "  assembly_dedup.purge_dups:\n"
        "    conda_env: test-env\n"
        "    required_tools: [purge_dups]\n"
        "  assembly_review.eval.quast:\n"
        "    conda_env: test-env\n"
        "    required_tools: [quast.py]\n"
        "  assembly_review.eval.busco:\n"
        "    conda_env: test-env\n"
        "    required_tools: [busco]\n",
        encoding="utf-8",
    )
    return path


def _append_database_registry(path: Path) -> None:
    """Add the same valid host database registry to a hand-written env fixture."""
    busco_root = path.parent / "busco"
    ensure_busco_lineage(busco_root)
    path.write_text(
        path.read_text(encoding="utf-8") + "databases:\n"
        "  busco:\n"
        "    default_lineage: embryophyta_odb10\n"
        "    lineages:\n"
        "      embryophyta_odb10:\n"
        f"        path: {busco_root / 'embryophyta_odb10'}\n"
        "        version: odb10\n"
        "        source: test_fixture\n"
        "  ncbi_metazoa:\n"
        f"    reference_dir: {_write_ncbi_reference(path.parent)}\n",
        encoding="utf-8",
    )


def _assembly_selected_closure(run_config: RunConfig) -> dict[str, tuple[str, ...]]:
    """Inject a stage-local Assembly choice without putting it in RunConfig."""
    closure = effective_method_closure(run_config)
    closure["assembly"] = ("assembly.candidate.flye",)
    return closure


def test_local_method_closure_uses_only_selected_assembler() -> None:
    methods = [
        method
        for methods in _assembly_selected_closure(_assembly_config()).values()
        for method in methods
    ]

    assert "assembly.candidate.flye" in methods
    assert "assembly.candidate.hifiasm" not in methods
    assert "assembly.candidate.nextdenovo" not in methods
    assert "assembly_dedup.purge_dups" in methods
    assert "assembly_review.eval.quast" in methods
    assert "assembly_review.eval.busco" in methods


def test_deferred_assembly_closure_waits_for_stage_recommendation() -> None:
    run_config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly", "allow_local_execution": True},
                {"stage": "assembly_dedup", "allow_local_execution": True},
            ]
        }
    )

    closure = effective_method_closure(run_config)

    assert "assembly" not in closure
    assert closure["assembly_dedup"] == ("assembly_dedup.purge_dups",)
    assert set(assembly_candidate_method_scope()) == {
        "assembly.candidate.flye",
        "assembly.candidate.nextdenovo",
        "assembly.candidate.hifiasm",
        "assembly.candidate.hifiasm_ont",
    }


def test_full_deterministic_assembly_does_not_require_llm() -> None:
    run_config = RunConfig.model_validate(
        {
            "requested_stages": [{"stage": "assembly", "allow_local_execution": True}],
            "assembly": {
                "method_route_advisor": "deterministic_baseline",
                "parameter_advisor": "deterministic_baseline",
            },
        }
    )

    assert not run_requires_llm(run_config)


def test_partial_deterministic_assembly_still_requires_llm() -> None:
    run_config = RunConfig.model_validate(
        {
            "requested_stages": [{"stage": "assembly", "allow_local_execution": True}],
            "assembly": {
                "method_route_advisor": "advisor",
                "parameter_advisor": "deterministic_baseline",
            },
        }
    )

    assert run_requires_llm(run_config)


def test_deferred_assembly_does_not_preflight_all_candidates(tmp_path: Path) -> None:
    run_config = RunConfig.model_validate(
        {"requested_stages": [{"stage": "assembly", "allow_local_execution": True}]}
    )
    probed_commands: list[list[str]] = []

    preflight_run_readiness(
        run_config,
        effective_methods=effective_method_closure(run_config),
        config_path=_env_config(tmp_path / "envs.yaml"),
        runner=lambda command, **_kwargs: (
            probed_commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
        llm_probe=lambda: None,
    )

    assert probed_commands == []


def test_preflight_probes_selected_methods_then_mandatory_llm(tmp_path: Path) -> None:
    probed_commands: list[list[str]] = []
    llm_calls: list[str] = []

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        probed_commands.append(command)
        stdout = "BUSCO 6.1.0\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config()
    closure = _assembly_selected_closure(run_config)
    methods = preflight_run_readiness(
        run_config,
        effective_methods=closure,
        config_path=_env_config(tmp_path / "envs.yaml"),
        runner=runner,
        llm_probe=lambda: llm_calls.append("called"),
    )

    assert "assembly.candidate.flye" in methods
    assert all("hifiasm" not in command for command in probed_commands)
    assert any(command[-2:] == ["busco", "--version"] for command in probed_commands)
    assert llm_calls == ["called"]


def test_odb12_2_rejects_busco_before_6_1(tmp_path: Path) -> None:
    llm_calls: list[str] = []

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        stdout = "BUSCO 6.0.0\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config(lineage_version="odb12.2")
    with pytest.raises(
        RunReadinessError,
        match=r"BUSCO 6\.0\.0 does not satisfy >=6\.1 required for "
        r"lineage_version=odb12\.2",
    ):
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=_env_config(tmp_path / "envs.yaml"),
            runner=runner,
            llm_probe=lambda: llm_calls.append("called"),
        )

    assert llm_calls == []


def test_odb12_2_accepts_busco_6_1(tmp_path: Path) -> None:
    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        stdout = "BUSCO 6.1.0\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config(lineage_version="odb12.2")
    methods = preflight_run_readiness(
        run_config,
        effective_methods=_assembly_selected_closure(run_config),
        config_path=_env_config(tmp_path / "envs.yaml"),
        runner=runner,
        llm_probe=lambda: None,
    )

    assert "assembly_review.eval.busco" in methods


def test_odb12_rejects_busco_before_5_8(tmp_path: Path) -> None:
    llm_calls: list[str] = []

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        stdout = "BUSCO 5.7.1\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config(lineage_version="odb12")
    with pytest.raises(
        RunReadinessError,
        match=r"BUSCO 5\.7\.1 does not satisfy >=5\.8 required for "
        r"lineage_version=odb12",
    ):
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=_env_config(tmp_path / "envs.yaml"),
            runner=runner,
            llm_probe=lambda: llm_calls.append("called"),
        )

    assert llm_calls == []


def test_odb12_accepts_busco_5_8(tmp_path: Path) -> None:
    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        stdout = "BUSCO 5.8.0\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config(lineage_version="odb12")
    methods = preflight_run_readiness(
        run_config,
        effective_methods=_assembly_selected_closure(run_config),
        config_path=_env_config(tmp_path / "envs.yaml"),
        runner=runner,
        llm_probe=lambda: None,
    )

    assert "assembly_review.eval.busco" in methods


def test_odb10_accepts_descriptor_minimum_busco_5_4(tmp_path: Path) -> None:
    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        stdout = "BUSCO 5.4.0\n" if command[-2:] == ["busco", "--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    run_config = _assembly_config(lineage_version="odb10")
    methods = preflight_run_readiness(
        run_config,
        effective_methods=_assembly_selected_closure(run_config),
        config_path=_env_config(tmp_path / "envs.yaml"),
        runner=runner,
        llm_probe=lambda: None,
    )

    assert "assembly_review.eval.busco" in methods


def test_local_method_failure_stops_before_llm_probe(tmp_path: Path) -> None:
    llm_calls: list[str] = []

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "missing")

    with pytest.raises(RunReadinessError, match="local method readiness failed"):
        run_config = _assembly_config()
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=_env_config(tmp_path / "envs.yaml"),
            runner=runner,
            llm_probe=lambda: llm_calls.append("called"),
        )
    assert llm_calls == []


def test_selected_method_rejects_zero_requirement_stage_fallback(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "envs.yaml"
    config_path.write_text(
        "default:\n"
        "  conda_bin: /usr/bin/true\n"
        "  fallback_env: test-env\n"
        "stages:\n"
        "  assembly:\n"
        "    conda_env: test-env\n"
        "packages: {}\n"
        "methods:\n"
        "  assembly_dedup.purge_dups:\n"
        "    required_tools: [purge_dups]\n"
        "  assembly_review.eval.quast:\n"
        "    required_tools: [quast.py]\n"
        "  assembly_review.eval.busco:\n"
        "    required_tools: [busco]\n",
        encoding="utf-8",
    )
    _append_database_registry(config_path)
    run_config = _assembly_config()

    with pytest.raises(
        RunReadinessError,
        match=(
            "assembly\\.candidate\\.flye: resolved env mapping declares no "
            "required_tools or package_tools"
        ),
    ):
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=config_path,
            runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, "BUSCO 6.1.0\n", ""
            ),
            llm_probe=lambda: None,
        )


def test_selected_method_accepts_stage_level_requirement_mapping(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "envs.yaml"
    config_path.write_text(
        "default:\n"
        "  conda_bin: /usr/bin/true\n"
        "  fallback_env: test-env\n"
        "stages:\n"
        "  assembly:\n"
        "    conda_env: test-env\n"
        "    required_tools: [flye]\n"
        "packages: {}\n"
        "methods:\n"
        "  assembly_dedup.purge_dups:\n"
        "    required_tools: [purge_dups]\n"
        "  assembly_review.eval.quast:\n"
        "    required_tools: [quast.py]\n"
        "  assembly_review.eval.busco:\n"
        "    required_tools: [busco]\n",
        encoding="utf-8",
    )
    _append_database_registry(config_path)
    run_config = _assembly_config()

    methods = preflight_run_readiness(
        run_config,
        effective_methods=_assembly_selected_closure(run_config),
        config_path=config_path,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            "BUSCO 6.1.0\n" if command[-2:] == ["busco", "--version"] else "",
            "",
        ),
        llm_probe=lambda: None,
    )

    assert "assembly.candidate.flye" in methods


def test_selected_method_rejects_empty_resolved_requirement_mapping(
    tmp_path: Path,
) -> None:
    config_path = _env_config(tmp_path / "envs.yaml")
    document = config_path.read_text(encoding="utf-8").replace(
        "    required_tools: [flye]\n",
        "    required_tools: []\n",
    )
    config_path.write_text(document, encoding="utf-8")
    run_config = _assembly_config()

    with pytest.raises(
        RunReadinessError,
        match=(
            "assembly\\.candidate\\.flye: resolved env mapping declares no "
            "required_tools or package_tools"
        ),
    ):
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=config_path,
            runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, "BUSCO 6.1.0\n", ""
            ),
            llm_probe=lambda: None,
        )


def test_review_only_path_does_not_require_llm(tmp_path: Path) -> None:
    review_only = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly_review", "allow_local_execution": False}
            ],
            "assembly_review": {
                "busco": {"lineage": "embryophyta_odb10", "threads": 4}
            },
        }
    )
    assert not run_requires_llm(review_only)
    assert (
        preflight_run_readiness(
            review_only,
            effective_methods=effective_method_closure(review_only),
            config_path=_env_config(tmp_path / "envs.yaml"),
            llm_probe=lambda: pytest.fail(),
        )
        == []
    )
    assert run_requires_llm(_assembly_config(review_local=False))


def test_inventory_only_still_validates_dependency_configuration(
    tmp_path: Path,
) -> None:
    run_config = RunConfig(requested_stages=[])
    missing_config = tmp_path / "missing-envs.yaml"

    with pytest.raises(
        RunReadinessError,
        match="dependency configuration is not ready",
    ):
        preflight_run_readiness(
            run_config,
            effective_methods=_assembly_selected_closure(run_config),
            config_path=missing_config,
        )


def test_review_closure_canonicalizes_quast_only_selection_to_fixed_pipeline() -> None:
    run_config = _assembly_config()

    closure = effective_method_closure(
        run_config,
        selected_methods={"assembly_review": ["assembly_review.eval.quast"]},
    )

    assert closure["assembly_review"] == (
        "assembly_review.eval.quast",
        "assembly_review.eval.busco",
    )
