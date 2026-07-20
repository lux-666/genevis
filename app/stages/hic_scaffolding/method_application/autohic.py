"""AutoHiC adapter for chromosome-scale Hi-C scaffolding and correction."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import persist_frozen_attempt
from app.execution.method_runner import build_frozen_attempt
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    update_current_parameter_attempt,
)
from app.schemas.artifacts import (
    HIC_ASSEMBLY_FILE,
    HIC_CONTACT_MAP,
    HIC_HTML_REPORT,
    HIC_PARAMETER_FROZEN_PARAMETERS,
    ParameterAttempt,
)
from app.configuration import load_descriptor_registry
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.stages.hic_scaffolding.method_application.lifecycle import (
    HiCMethodLifecycle,
    build_method_graph,
    collect_method_artifacts,
    hic_output,
    update_hic_output,
)
from app.stages.hic_scaffolding.parameters.storage import (
    AUTOHIC_PARAMETER_COMMAND,
    AUTOHIC_PARAMETER_CONFIG,
    AUTOHIC_PARAMETER_SCRIPT,
    AutoHiCParameterPersistenceError,
    publish_autohic_parameter_file,
    write_autohic_parameter_artifact,
)
from app.stages.hic_scaffolding.parameters.workflow import (
    load_approved_autohic_parameters,
)
from app.storage.paths import SessionPaths
from app.storage.publish import shell_publish_variable

METHOD = "hic_scaffolding.candidate.autohic"
LIFECYCLE = HiCMethodLifecycle(
    name="autohic",
    method_name=METHOD,
    frozen_status="autohic_frozen",
    ran_status="autohic_ran",
)


def build_autohic_graph():
    """Build the AutoHiC freeze -> run -> collect lifecycle."""
    return build_method_graph(LIFECYCLE, freeze_autohic_attempt, collect_autohic_artifacts)


def freeze_autohic_attempt(state: TopLevelState) -> dict:
    """Freeze one end-to-end AutoHiC run from contigs and paired Hi-C reads."""
    output = hic_output(state)
    if not is_enabled(output):
        return {}
    if METHOD not in load_descriptor_registry().methods:
        return update_hic_output(
            state,
            _missing_block(f"descriptor: {METHOD}"),
        )

    assembly = Path(str(output.get("pre_hic_assembly_ref", "")))
    if not assembly.is_file():
        return _blocked(state, output, "pre_hic_contig_assembly_not_materialized_for_autohic")

    parameters, blocked = _parameters(state, output, assembly)
    if blocked:
        return update_hic_output(state, blocked)

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    env_spec, packages, env_blocked = resolve_method_env(
        output,
        stage="hic_scaffolding",
        method_name=METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_hic_output(
            state,
            _missing_block(str(env_blocked.get("reason") or "AutoHiC environment")),
        )

    package_by_name = {package.name: package for package in packages}
    missing_packages = [name for name in ("autohic", "juicer", "three_d_dna") if name not in package_by_name]
    if missing_packages:
        return update_hic_output(state, _missing_block("package_tools: " + ", ".join(missing_packages)))

    autohic_root = Path(package_by_name["autohic"].install_root)
    juicer_root = Path(package_by_name["juicer"].install_root)
    three_d_dna_root = Path(package_by_name["three_d_dna"].install_root)
    autohic_entry = autohic_root / "autohic.py"
    if not autohic_entry.is_file():
        return update_hic_output(state, _missing_tool_block("autohic.py", autohic_root))

    error_model = package_by_name["autohic"].model_path("error")
    chromosome_model = package_by_name["autohic"].model_path("chromosome")
    missing_models: list[str] = []
    if error_model is None or not error_model.is_file():
        missing_models.append("packages.autohic.models.error")
    if chromosome_model is None or not chromosome_model.is_file():
        missing_models.append("packages.autohic.models.chromosome")
    if missing_models:
        return update_hic_output(state, _missing_block(", ".join(missing_models)))
    parameters["error_model_ref"] = str(error_model.resolve())
    parameters["chromosome_model_ref"] = str(chromosome_model.resolve())

    try:
        parameter_attempt = current_parameter_attempt(
            output,
            method_id=METHOD,
            label=LIFECYCLE.name,
        )
        if parameter_attempt.status != "recommended":
            raise AutoHiCParameterPersistenceError(
                "AutoHiC current parameter attempt is not approved for freeze"
            )
        frozen_parameters_ref = write_autohic_parameter_artifact(
            state,
            parameter_attempt.attempt_id,
            "frozen_parameters.json",
            {
                "schema_version": 2,
                "stage": "hic_scaffolding",
                "method_id": METHOD,
                "recommendation_manifest_ref": (
                    parameter_attempt.recommendation_manifest_ref.model_dump(
                        mode="json"
                    )
                ),
                "approval_decision_ref": (
                    parameter_attempt.approval_decision_ref.model_dump(mode="json")
                ),
                "parameters": parameters,
            },
            artifact_type=HIC_PARAMETER_FROZEN_PARAMETERS,
            method_name=METHOD,
            scope="attempt",
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "parameter_freeze",
                "reason": f"AutoHiC parameters could not be frozen: {exc}",
            },
        )

    working_dir = stage_work_dir(state, "hic_scaffolding", "autohic")
    artifact_dir = stage_artifacts_dir(state, "hic_scaffolding", "autohic")
    reference_dir = working_dir / "input" / "references"
    fastq_root = working_dir / "input" / "rawdata"
    fastq_dir = fastq_root / "fastq"
    result_root = working_dir / "results"
    job_name = "AutoHiC"
    top_result = result_root / job_name
    input_assembly = reference_dir / "contigs.fasta"
    config_path = working_dir / "cfg-autohic.txt"
    scaffold_fasta = artifact_dir / "chromosome_scaffolded_assembly.fasta"
    contact_map = artifact_dir / "contact_map.hic"
    assembly_file = artifact_dir / "chromosome.assembly"
    html_report = artifact_dir / "result.html"
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Freeze may be replayed after a crash between publication and checkpoint.
    # Remove only mutable work links/files before regenerating exact bytes; the
    # immutable attempt artifacts themselves are reused or conflict-checked.
    config_path.unlink(missing_ok=True)
    config_path.write_text(
        _config_text(
            parameters=parameters,
            autohic_root=autohic_root,
            juicer_root=juicer_root,
            three_d_dna_root=three_d_dna_root,
            result_root=result_root,
            job_name=job_name,
            input_assembly=input_assembly,
            fastq_root=fastq_root,
        ),
        encoding="utf-8",
    )
    try:
        config_ref = publish_autohic_parameter_file(
            state,
            parameter_attempt.attempt_id,
            "cfg-autohic.txt",
            str(config_path),
            artifact_type=AUTOHIC_PARAMETER_CONFIG,
            method_name=METHOD,
        )
        published_config_path = SessionPaths.from_run_dir(data_dir).resolve_ref(
            config_ref.path_ref
        )
        config_path.symlink_to(published_config_path)
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "parameter_freeze",
                "reason": f"AutoHiC config could not be published: {exc}",
            },
        )
    script = working_dir / "run_autohic.sh"
    script.unlink(missing_ok=True)
    script.write_text(
        _script(
            assembly=assembly,
            hic_read_paths=parameters["hic_read_paths"],
            autohic_root=autohic_root,
            autohic_entry=autohic_entry,
            python_command=parameters["python_command"],
            reference_dir=reference_dir,
            fastq_dir=fastq_dir,
            result_root=result_root,
            top_result=top_result,
            input_assembly=input_assembly,
            config_path=published_config_path,
            scaffold_fasta=scaffold_fasta,
            contact_map=contact_map,
            assembly_file=assembly_file,
            html_report=html_report,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)

    try:
        script_ref = publish_autohic_parameter_file(
            state,
            parameter_attempt.attempt_id,
            "run_autohic.sh",
            str(script),
            artifact_type=AUTOHIC_PARAMETER_SCRIPT,
            method_name=METHOD,
        )
        published_script = SessionPaths.from_run_dir(data_dir).resolve_ref(
            script_ref.path_ref
        )
        script.symlink_to(published_script)
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "parameter_freeze",
                "reason": f"AutoHiC script could not be published: {exc}",
            },
        )

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:hic_scaffolding:autohic",
        session_id=session_id,
        stage="hic_scaffolding",
        method_name=METHOD,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[scaffold_fasta, contact_map, assembly_file, html_report],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "artifact_dir": str(artifact_dir),
            "autohic_result_dir": str(top_result),
            "scaffold_fasta": str(scaffold_fasta),
            "contact_map": str(contact_map),
            "assembly_file": str(assembly_file),
            "html_report": str(html_report),
            "workflow": [
                "juicer_mapping",
                "3d_dna_initial_scaffolding",
                "deep_learning_error_detection",
                "assembly_correction",
                "3d_dna_rerun",
                "chromosome_assignment",
                "html_report_generation",
            ],
        },
    )
    try:
        frozen_attempt_ref = persist_frozen_attempt(state, attempt)
        command_ref = write_autohic_parameter_artifact(
            state,
            parameter_attempt.attempt_id,
            "command.json",
            {
                "schema_version": 2,
                "stage": "hic_scaffolding",
                "method_id": METHOD,
                "frozen_parameters_ref": frozen_parameters_ref.model_dump(mode="json"),
                "config_ref": config_ref.model_dump(mode="json"),
                "script_ref": script_ref.model_dump(mode="json"),
            },
            artifact_type=AUTOHIC_PARAMETER_COMMAND,
            method_name=METHOD,
            scope="attempt",
        )
        parameter_attempts = update_current_parameter_attempt(
            output,
            method_id=METHOD,
            label=LIFECYCLE.name,
            status="frozen",
            frozen_parameters_ref=frozen_parameters_ref.model_dump(mode="json"),
            command_ref=command_ref.model_dump(mode="json"),
            frozen_attempt_ref=frozen_attempt_ref.model_dump(mode="json"),
        )
    except (
        OSError,
        ValueError,
        AutoHiCParameterPersistenceError,
        ParameterAttemptStateError,
    ) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "parameter_freeze",
                "reason": f"AutoHiC command could not be frozen: {exc}",
            },
        )
    return update_hic_output(
        state,
        {
            "status": LIFECYCLE.frozen_status,
            "parameter_attempts": parameter_attempts,
        },
    )


def collect_autohic_artifacts(state: TopLevelState) -> dict:
    """Register AutoHiC's chromosome FASTA, .hic, .assembly, and report."""
    output = hic_output(state)
    parameter_attempts = [
        ParameterAttempt.model_validate(item)
        for item in (output.get("parameter_attempts") or [])
        if isinstance(item, dict)
        and item.get("attempt_id") == output.get("current_parameter_attempt_id")
    ]
    execution_metadata: dict[str, Any] = {"pipeline": "AutoHiC"}
    if len(parameter_attempts) == 1:
        parameter_attempt = parameter_attempts[0]
        execution_metadata.update(
            {
                "frozen_parameters_ref": (
                    parameter_attempt.frozen_parameters_ref.model_dump(mode="json")
                    if parameter_attempt.frozen_parameters_ref is not None
                    else None
                ),
                "command_ref": (
                    parameter_attempt.command_ref.model_dump(mode="json")
                    if parameter_attempt.command_ref is not None
                    else None
                ),
                "execution_ref": (
                    parameter_attempt.execution_ref.model_dump(mode="json")
                    if parameter_attempt.execution_ref is not None
                    else None
                ),
            }
        )
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[
            ("contact_map", HIC_CONTACT_MAP),
            ("assembly_file", HIC_ASSEMBLY_FILE),
            ("html_report", HIC_HTML_REPORT),
        ],
        execution_metadata=execution_metadata,
    )


def is_enabled(output: dict[str, Any]) -> bool:
    """Return whether selected-stage execution has approved frozen parameters."""

    return (
        output.get("execution_mode") == "local"
        and output.get("status") == "parameters_approved"
    )


def _parameters(
    state: TopLevelState,
    output: dict[str, Any],
    assembly: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw = dict(output.get("autohic_parameters") or {})
    try:
        approved = load_approved_autohic_parameters(state)
    except (ValueError, AutoHiCParameterPersistenceError) as exc:
        return {}, _missing_block(f"approved AutoHiC parameter attempt: {exc}")
    enzyme = str(approved.get("restriction_enzyme") or "").strip()
    if not enzyme:
        return {}, _missing_block("approved restriction_enzyme")
    if any(character in enzyme for character in ("\n", "\r", "=")):
        return {}, _parameter_block(
            "restriction_enzyme",
            enzyme,
            "cannot contain newline or '=' characters in an AutoHiC config",
        )

    reads = [Path(str(path)) for path in output.get("hic_read_paths") or []]
    if len(reads) < 2 or len(reads) % 2:
        return {}, _missing_block("paired hic_read_paths (R1/R2)")
    missing_reads = [str(path) for path in reads if not path.is_file()]
    if missing_reads:
        return {}, _parameter_block("hic_read_paths", missing_reads, "all paired Hi-C reads must be materialized")

    threads = _positive_int(raw.get("threads", 10))
    if threads is None:
        return {}, _parameter_block("autohic_parameters.threads", raw.get("threads"), "must be a positive integer")
    number_of_edit_rounds = _nonnegative_int(raw.get("number_of_edit_rounds", 2))
    if number_of_edit_rounds is None:
        return {}, _parameter_block(
            "autohic_parameters.number_of_edit_rounds",
            raw.get("number_of_edit_rounds"),
            "must be a non-negative integer",
        )
    error_max_epoch = _positive_int(raw.get("error_max_epoch", 5))
    error_min_len = _positive_int(raw.get("error_min_len", 15_000))
    error_max_len = _positive_int(raw.get("error_max_len", 20_000_000))
    if error_max_epoch is None or error_min_len is None or error_max_len is None:
        return {}, _parameter_block("autohic_parameters.error_limits", raw, "epoch and length limits must be positive integers")
    if error_max_len < error_min_len:
        return {}, _parameter_block("autohic_parameters.error_max_len", error_max_len, "must be >= error_min_len")

    error_filter_score = _probability(raw.get("error_filter_score", 0.9))
    error_filter_iou_score = _probability(raw.get("error_filter_iou_score", 0.8))
    if error_filter_score is None:
        return {}, _parameter_block(
            "autohic_parameters.error_filter_score",
            raw.get("error_filter_score"),
            "must be a number between 0 and 1",
        )
    if error_filter_iou_score is None:
        return {}, _parameter_block(
            "autohic_parameters.error_filter_iou_score",
            raw.get("error_filter_iou_score"),
            "must be a number between 0 and 1",
        )

    python_command = str(raw.get("python_command") or "python3.9").strip()
    if not python_command:
        return {}, _parameter_block("autohic_parameters.python_command", python_command, "must be non-empty")

    return {
        "species_name": _safe_name(str(output.get("species_name") or assembly.stem)),
        "hic_read_paths": [str(path.resolve()) for path in reads],
        "restriction_enzyme": enzyme,
        "threads": threads,
        "number_of_edit_rounds": number_of_edit_rounds,
        "translocation_adjust": bool(raw.get("translocation_adjust", True)),
        "inversion_adjust": bool(raw.get("inversion_adjust", True)),
        "debris_adjust": bool(raw.get("debris_adjust", False)),
        "error_max_epoch": error_max_epoch,
        "error_min_len": error_min_len,
        "error_max_len": error_max_len,
        "error_filter_score": error_filter_score,
        "error_filter_iou_score": error_filter_iou_score,
        "python_command": python_command,
    }, None


def _config_text(
    *,
    parameters: dict[str, Any],
    autohic_root: Path,
    juicer_root: Path,
    three_d_dna_root: Path,
    result_root: Path,
    job_name: str,
    input_assembly: Path,
    fastq_root: Path,
) -> str:
    """Render the exact key set accepted by AutoHiC's cfg parser."""
    values = [
        ("JOB_NAME", job_name),
        ("AutoHiC_DIR", autohic_root),
        ("RESULT_DIR", result_root),
        ("N_CPU", parameters["threads"]),
        ("SPECIES_NAME", parameters["species_name"]),
        ("REFERENCE_GENOME", input_assembly),
        ("JUICER_DIR", juicer_root),
        ("FASTQ_DIR", fastq_root),
        ("ENZYME", parameters["restriction_enzyme"]),
        ("TD_DNA_DIR", three_d_dna_root),
        ("NUMBER_OF_EDIT_ROUNDS", parameters["number_of_edit_rounds"]),
        ("ERROR_PRETRAINED_MODEL", parameters["error_model_ref"]),
        ("CHR_PRETRAINED_MODEL", parameters["chromosome_model_ref"]),
        ("TRANSLOCATION_ADJUST", _autohic_bool(parameters["translocation_adjust"])),
        ("INVERSION_ADJUST", _autohic_bool(parameters["inversion_adjust"])),
        ("DEBRIS_ADJUST", _autohic_bool(parameters["debris_adjust"])),
        ("ERROR_MAX_EPOCH", parameters["error_max_epoch"]),
        ("ERROR_MIN_LEN", parameters["error_min_len"]),
        ("ERROR_MAX_LEN", parameters["error_max_len"]),
        ("ERROR_FILTER_SCORE", parameters["error_filter_score"]),
        ("ERROR_FILTER_IOU_SCORE", parameters["error_filter_iou_score"]),
    ]
    return "".join(f"{key}={value}\n" for key, value in values)


def _script(
    *,
    assembly: Path,
    hic_read_paths: list[str],
    autohic_root: Path,
    autohic_entry: Path,
    python_command: str,
    reference_dir: Path,
    fastq_dir: Path,
    result_root: Path,
    top_result: Path,
    input_assembly: Path,
    config_path: Path,
    scaffold_fasta: Path,
    contact_map: Path,
    assembly_file: Path,
    html_report: Path,
) -> str:
    lines = [
        "set -e",
        f"mkdir -p {shlex.quote(str(reference_dir))} {shlex.quote(str(fastq_dir))} {shlex.quote(str(result_root))}",
        f"cp {shlex.quote(str(assembly))} {shlex.quote(str(input_assembly))}",
    ]
    for index, read_path in enumerate(hic_read_paths):
        pair_index = index // 2 + 1
        mate = "R1" if index % 2 == 0 else "R2"
        suffix = ".fastq.gz" if read_path.lower().endswith(".gz") else ".fastq"
        normalized = fastq_dir / f"hic_{pair_index:03d}_{mate}{suffix}"
        lines.append(f"ln -sfn {shlex.quote(read_path)} {shlex.quote(str(normalized))}")
    lines.extend(
        [
            f"test ! -e {shlex.quote(str(top_result))}",
            f"cd {shlex.quote(str(autohic_root))}",
            " ".join(
                shlex.quote(value)
                for value in [python_command, str(autohic_entry), "-c", str(config_path)]
            ),
            "candidate=''",
            "for value in "
            + " ".join(
                shlex.quote(str(path))
                for path in [
                    top_result / "contigs_autohic.fasta",
                    top_result / "autohic_results" / "chromosome" / "contigs_autohic.fasta",
                    top_result / "autohic_results" / "chromosome" / "contigs.FINAL.fasta",
                ]
            )
            + "; do if test -f \"$value\"; then candidate=\"$value\"; break; fi; done",
            "test -n \"$candidate\"",
            shell_publish_variable("candidate", scaffold_fasta),
            f"candidate={shlex.quote(str(top_result / 'autohic_results' / 'chromosome' / 'contigs.final.hic'))}",
            "test -f \"$candidate\"",
            shell_publish_variable("candidate", contact_map),
            f"candidate={shlex.quote(str(top_result / 'autohic_results' / 'chromosome' / 'contigs.final.assembly'))}",
            "test -f \"$candidate\"",
            shell_publish_variable("candidate", assembly_file),
            f"candidate={shlex.quote(str(top_result / 'result.html'))}",
            "test -f \"$candidate\"",
            shell_publish_variable("candidate", html_report),
        ]
    )
    return "\n".join(lines) + "\n"


def _positive_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _probability(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if 0.0 <= converted <= 1.0 else None


def _safe_name(value: str) -> str:
    normalized = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return normalized.strip("._-") or "GeneAgent"


def _autohic_bool(value: bool) -> str:
    return "True" if value else "False"


def _missing_block(field: str) -> dict[str, Any]:
    return {
        "status": "blocked_missing_requirements",
        "missing_requirements": [field],
        "reason": f"AutoHiC requirement is missing: {field}",
        "method_name": METHOD,
    }


def _parameter_block(field: str, value: Any, reason: str) -> dict[str, Any]:
    return {
        "status": "blocked_missing_requirements",
        "missing_requirements": [],
        "invalid_params": [{"param": field, "value": value, "reason": reason}],
        "reason": f"Invalid AutoHiC requirement {field}: {reason}",
        "method_name": METHOD,
    }


def _missing_tool_block(tool: str, root: Path) -> dict[str, Any]:
    return {
        "status": "blocked_missing_requirements",
        "reason": f"{tool} missing under {root}",
        "env_target": str(root),
        "missing_requirements": [tool],
        "method_name": METHOD,
    }


def _blocked(state: TopLevelState, output: dict[str, Any], note: str) -> dict:
    return update_hic_output(
        state,
        {
            "status": "blocked_missing_requirements",
            "reason": note,
            "missing_requirements": [note],
            "notes": list(dict.fromkeys([*(output.get("notes") or []), note])),
        },
    )
