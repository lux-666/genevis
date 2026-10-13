"""Every executable stage must resolve from its committed dependency contract."""

import json
import subprocess

import pytest

from app.configuration import load_descriptor_registry
from app.execution.dependency_snapshot import load_frozen_dependency_resolver
from app.execution.env_dispatch import resolve_method_env
from app.execution.env_resolver import preflight
from app.execution.run_readiness import (
    RunReadinessError,
    effective_method_closure,
    preflight_run_readiness,
)
from app.schemas.run_config import RunConfig
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.paths import SessionPaths
from tests.busco_fixture import ensure_test_host_envs

# Expected executable identities are independent of the plan projection under test.
CASES = [
    ("read_qc", {}, ("read_qc.long_read_stats.seqkit_stats",)),
    (
        "read_qc",
        {
            "short_reads": {},
            "hic_reads": {},
            "length_filter": {"approved": True, "thresholds": {"min_len": 1000}},
        },
        (
            "read_qc.long_read_stats.seqkit_stats",
            "read_qc.length_filter.seqkit_seq",
            "read_qc.short_reads.fastp",
            "read_qc.hic_reads.fastp",
        ),
    ),
    (
        "genome_survey",
        {"survey_parameters": {"kmer_size": 21, "hash_size": "1G"}},
        ("genome_survey.kmer_count.jellyfish", "genome_survey.model.genomescope2"),
    ),
    (
        "assembly",
        {},
        (
            "assembly.candidate.flye",
            "assembly.candidate.hifiasm",
            "assembly.candidate.hifiasm_ont",
            "assembly.candidate.nextdenovo",
        ),
    ),
    ("assembly_dedup", {}, ("assembly_dedup.purge_dups",)),
    ("assembly_dedup", None, ("assembly_dedup.purge_dups",)),
    (
        "assembly_dedup",
        {"recommendation_mode": "advisor", "automatic_purge_approved": False},
        ("assembly_dedup.purge_dups",),
    ),
    (
        "assembly_dedup",
        {"recommendation_mode": "advisor", "automatic_purge_approved": True},
        ("assembly_dedup.purge_dups", "assembly_dedup.eval.quast"),
    ),
    (
        "assembly_review",
        {},
        ("assembly_review.eval.quast", "assembly_review.eval.busco"),
    ),
    ("hic_scaffolding", {}, ("hic_scaffolding.candidate.autohic",)),
    *[
        (stage, {"recommendation_mode": mode}, methods)
        for stage, executable in (
            ("gapcloser", "gapcloser.tgs_gapcloser"),
            ("t2t", "t2t.alignment.minimap2"),
            ("base_polish", "base_polish.racon"),
        )
        for mode, methods in (
            ("advisor", (f"{stage}.eval.quast", executable)),
            ("deterministic_baseline", (executable,)),
        )
    ],
    *[
        (stage, None, (f"{stage}.eval.quast", executable))
        for stage, executable in (
            ("gapcloser", "gapcloser.tgs_gapcloser"),
            ("t2t", "t2t.alignment.minimap2"),
            ("base_polish", "base_polish.racon"),
        )
    ],
]


@pytest.fixture
def inputs(tmp_path):
    envs = ensure_test_host_envs(tmp_path)
    assets = []
    for name, asset_type, platform, fmt in (
        ("reads.fq", "raw_long_reads", "pacbio_hifi", "fastq"),
        ("assembly.fa", "assembly_fasta_registered", None, "fasta"),
    ):
        path = tmp_path / name
        path.write_text(">ctg\nACGT\n" if fmt == "fasta" else "@r\nACGT\n+\nIIII\n")
        assets.append(
            {"asset_type": asset_type, "paths": [str(path)], "format": fmt}
            | ({"platform": platform} if platform else {})
        )
    for asset_type in ("raw_short_dna_reads", "hic_reads_registered"):
        mates = [tmp_path / f"{asset_type}_{mate}.fq" for mate in (1, 2)]
        for path in mates:
            path.write_text("@r\nACGT\n+\nIIII\n")
        assets.append(
            {
                "asset_type": asset_type,
                "paired_reads": [{"r1": str(mates[0]), "r2": str(mates[1])}],
                "platform": "illumina",
                "format": "fastq",
            }
        )
    return envs, {
        "session_id": "stage-scope",
        "user_metadata": {"target_species": "Fixture species"},
        "genome_size_estimate": 1000,
        "assets": assets,
    }


def _config(stage, settings, authorizations):
    return RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": stage, "allow_local_execution": local}
                for local in authorizations
            ],
            "shared_parameters": {"species_facts": {"telomere_motif": "TTAGGG"}},
            **({stage: settings} if settings is not None else {}),
        }
    )


def _host_mapping(envs, stage):
    payload = json.loads(envs.read_text())
    # Stage inheritance must capture exact method identities even without overrides.
    payload["stages"][stage] = {
        "conda_env": f"{stage}-env",
        "required_tools": [f"{stage}-tool"],
        "package_tools": ["fixture-package"],
    }
    root = envs.parent / "package"
    root.mkdir(exist_ok=True)
    (root / "entry").write_text("fixture")
    payload["packages"]["fixture-package"] = {
        "install_root": str(root),
        "canary_paths": ["entry"],
    }
    envs.write_text(json.dumps(payload))


def _ready(command, **_kwargs):
    return subprocess.CompletedProcess(command, 0, "BUSCO 5.8.3\n", "")


@pytest.mark.parametrize("stage,settings,methods", CASES)
@pytest.mark.parametrize("authorizations", [(False,), (True,), (False, True, True)])
def test_every_stage_preflights_freezes_and_dispatches_without_live_config(
    tmp_path, monkeypatch, inputs, stage, settings, methods, authorizations
):
    if (
        not any(authorizations)
        and settings
        and (
            settings.get("automatic_purge_approved") or stage == "read_qc" and settings
        )
    ):
        with pytest.raises(ValueError, match="local_execution|local execution"):
            _config(stage, settings, authorizations)
        return
    config = _config(stage, settings, authorizations)
    envs, manifest = inputs
    _host_mapping(envs, stage)
    expected = set(methods) if any(authorizations) else set()
    closure = effective_method_closure(config)
    fixed = set().union(*map(set, closure.values()))
    assert fixed == (set() if stage == "assembly" else expected)
    checked = preflight_run_readiness(
        config,
        effective_methods=closure,
        species_name="Fixture species",
        config_path=envs,
        runner=_ready,
        llm_probe=lambda: None,
    )
    assert set(checked) == fixed
    assert len(checked) == len(fixed)
    paths = SessionPaths(tmp_path / "data", manifest["session_id"])
    freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=config,
        config_path=envs,
        probe_environments=False,
    )
    # The executor must retain its method/tools/package contract after host edits.
    envs.write_text("invalid live config")
    resolver = load_frozen_dependency_resolver(paths.run_dir)
    assert set(resolver.snapshot.selected_methods) == expected
    assert set(resolver.snapshot.method_environments) == expected
    monkeypatch.setattr(
        "app.execution.env_dispatch.preflight",
        lambda spec: preflight(spec, runner=_ready),
    )
    for method in expected:
        spec, packages, blocked = resolve_method_env(
            {},
            stage=stage,
            method_name=method,
            workflow_step_id=f"step-001-{stage}",
            session_id=paths.session_id,
            data_dir=paths.run_dir,
            input_manifest_ref="",
        )
        assert blocked is None
        assert spec.required_tools == [f"{stage}-tool"]
        assert spec.conda_env == f"{stage}-env"
        assert [package.name for package in packages] == ["fixture-package"]


@pytest.mark.parametrize("stage,settings,methods", CASES)
def test_missing_fixed_stage_tool_fails_before_freeze(inputs, stage, settings, methods):
    envs, _manifest = inputs
    _host_mapping(envs, stage)
    config = _config(stage, settings, (True,))

    def missing(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "fixture tool missing")

    if stage == "assembly":
        # Only the selected candidate is checked at stage entry.
        assert effective_method_closure(config) == {}
        return
    with pytest.raises(RunReadinessError) as rejected:
        preflight_run_readiness(
            config,
            effective_methods=effective_method_closure(config),
            species_name="Fixture species",
            config_path=envs,
            runner=missing,
            llm_probe=lambda: None,
        )
    for method in methods:
        assert method in str(rejected.value)
    assert not (envs.parent / "data").exists()


def test_scope_matrix_covers_every_registered_executable_method():
    covered = {method for _stage, _settings, methods in CASES for method in methods}
    registered = set(load_descriptor_registry().methods)
    assert covered == registered - {"read_qc.length_filter.skip"}


@pytest.mark.parametrize("stage,settings,methods", CASES)
def test_missing_fixed_stage_package_fails_before_freeze(
    inputs, stage, settings, methods
):
    if stage == "assembly":
        return
    envs, _manifest = inputs
    _host_mapping(envs, stage)
    (envs.parent / "package" / "entry").unlink()
    config = _config(stage, settings, (True,))
    with pytest.raises(RunReadinessError) as rejected:
        preflight_run_readiness(
            config,
            effective_methods=effective_method_closure(config),
            species_name="Fixture species",
            config_path=envs,
            runner=_ready,
            llm_probe=lambda: None,
        )
    for method in methods:
        assert method in str(rejected.value)
    assert "fixture-package" in str(rejected.value)
    assert not (envs.parent / "data").exists()


def test_configured_autohic_interpreter_is_checked_before_freeze(inputs):
    envs, _manifest = inputs
    _host_mapping(envs, "hic_scaffolding")
    config = _config(
        "hic_scaffolding",
        {"autohic_parameters": {"python_command": "custom-python"}},
        (True,),
    )

    def missing_interpreter(command, **kwargs):
        if command[-1] == "custom-python":
            return subprocess.CompletedProcess(command, 1, "", "missing interpreter")
        return _ready(command, **kwargs)

    with pytest.raises(RunReadinessError, match="custom-python"):
        preflight_run_readiness(
            config,
            effective_methods=effective_method_closure(config),
            config_path=envs,
            runner=missing_interpreter,
            llm_probe=lambda: None,
        )


@pytest.mark.parametrize("python_command", ["", "   "])
def test_empty_autohic_interpreter_is_rejected_in_run_config(python_command):
    with pytest.raises(ValueError, match="python_command must be non-empty"):
        _config(
            "hic_scaffolding",
            {"autohic_parameters": {"python_command": python_command}},
            (True,),
        )


@pytest.mark.parametrize(
    "python_command", ["python3.9", "custom-python", "/opt/tools/python", " python "]
)
def test_configured_autohic_interpreter_survives_freeze(
    tmp_path, inputs, python_command
):
    envs, manifest = inputs
    _host_mapping(envs, "hic_scaffolding")
    config = _config(
        "hic_scaffolding",
        {"autohic_parameters": {"python_command": python_command}},
        (True,),
    )
    paths = SessionPaths(tmp_path / "data", manifest["session_id"])
    freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=config,
        config_path=envs,
        probe_environments=False,
    )
    envs.unlink()
    spec = load_frozen_dependency_resolver(paths.run_dir).resolve(
        stage="hic_scaffolding", method_name="hic_scaffolding.candidate.autohic"
    )
    assert spec.required_tools == ["hic_scaffolding-tool", python_command.strip()]
