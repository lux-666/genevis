"""Verified access to immutable Assembly candidate bundles."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.integrity import sha256_file
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    HIC_SCAFFOLDED_ASSEMBLY,
    T2T_PATCHED_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
    AssemblyCandidateBundle,
)
from app.storage.artifacts import publish_artifact_record, resolve_artifact_ref
from app.storage.paths import SessionPaths


class AssemblyCandidateBundleError(ValueError):
    """An Assembly candidate ref or bundle failed closed validation."""


CANONICAL_ASSEMBLY_CANDIDATE_PRIMARY_TYPES: frozenset[str] = frozenset(
    {
        ASSEMBLY_DRAFT_ASSEMBLY,
        HIC_SCAFFOLDED_ASSEMBLY,
        T2T_PATCHED_ASSEMBLY,
    }
)
"""Artifact types permitted as the primary output of an Assembly candidate."""


@dataclass(frozen=True)
class VerifiedPlannedCandidate:
    """A verified planned candidate and its immutable bundle path."""

    ref: ArtifactRef
    bundle: AssemblyCandidateBundle
    path: Path


@dataclass(frozen=True)
class VerifiedExecutedCandidate:
    """A verified executed candidate, its plan, and canonical primary output."""

    ref: ArtifactRef
    bundle: AssemblyCandidateBundle
    path: Path
    plan_ref: ArtifactRef
    plan: AssemblyCandidateBundle
    plan_path: Path
    primary_output_ref: ArtifactRef
    primary_output_path: Path


def load_planned_candidate_bundle(
    data_dir: Path | str,
    session_id: str,
    value: ArtifactRef | dict[str, Any],
    *,
    expected_method_name: str | None = None,
    expected_candidate_id: str | None = None,
    expected_parameter_attempt_id: int | None = None,
) -> VerifiedPlannedCandidate:
    """Resolve and verify one indexed ``status=planned`` candidate bundle."""

    ref, bundle_path, bundle = _load_candidate_bundle(
        data_dir,
        session_id,
        value,
        expected_status="planned",
    )
    _validate_expected_identity(
        bundle,
        expected_method_name=expected_method_name,
        expected_candidate_id=expected_candidate_id,
        expected_parameter_attempt_id=expected_parameter_attempt_id,
    )
    return VerifiedPlannedCandidate(ref=ref, bundle=bundle, path=bundle_path)


def load_executed_candidate_bundle(
    data_dir: Path | str,
    session_id: str,
    value: ArtifactRef | dict[str, Any],
    *,
    expected_method_name: str | None = None,
    expected_candidate_id: str | None = None,
    expected_parameter_attempt_id: int | None = None,
) -> VerifiedExecutedCandidate:
    """Resolve and verify one executed candidate against its immutable plan."""

    ref, bundle_path, bundle = _load_candidate_bundle(
        data_dir,
        session_id,
        value,
        expected_status="executed",
    )
    _validate_expected_identity(
        bundle,
        expected_method_name=expected_method_name,
        expected_candidate_id=expected_candidate_id,
        expected_parameter_attempt_id=expected_parameter_attempt_id,
    )
    if bundle.plan_ref is None or bundle.primary_output is None:
        raise AssemblyCandidateBundleError(
            "executed candidate is missing its plan or primary output ref"
        )

    plan = load_planned_candidate_bundle(
        data_dir,
        session_id,
        bundle.plan_ref,
        expected_method_name=bundle.method_name,
        expected_candidate_id=bundle.candidate_id,
        expected_parameter_attempt_id=bundle.parameter_attempt_id,
    )
    _validate_plan_identity(bundle, plan.bundle)

    primary_output_ref = bundle.primary_output
    if (
        primary_output_ref.artifact_type
        not in CANONICAL_ASSEMBLY_CANDIDATE_PRIMARY_TYPES
    ):
        raise AssemblyCandidateBundleError(
            "executed candidate primary output has artifact type "
            f"{primary_output_ref.artifact_type!r}, expected one of "
            f"{sorted(CANONICAL_ASSEMBLY_CANDIDATE_PRIMARY_TYPES)!r}"
        )
    primary_output_path = _resolve_embedded_ref(
        data_dir,
        session_id,
        primary_output_ref,
        role="primary output",
    )
    if bundle.log_refs:
        expected_log_types = {
            "stdout": "command.stdout",
            "stderr": "command.stderr",
        }
        if set(bundle.log_refs) != set(expected_log_types):
            raise AssemblyCandidateBundleError(
                "executed candidate logs must be the canonical stdout/stderr pair"
            )
        for role, expected_type in expected_log_types.items():
            if bundle.log_refs[role].artifact_type != expected_type:
                raise AssemblyCandidateBundleError(
                    f"executed candidate {role} has artifact type "
                    f"{bundle.log_refs[role].artifact_type!r}, expected "
                    f"{expected_type!r}"
                )

    embedded_refs: list[tuple[str, ArtifactRef]] = [
        ("plan", bundle.plan_ref),
        ("primary output", primary_output_ref),
        *[
            (f"native output {output.native_role!r}", output.artifact_ref)
            for output in bundle.native_outputs
        ],
        *[
            (f"log {role!r}", log_ref)
            for role, log_ref in sorted(bundle.log_refs.items())
        ],
    ]
    artifact_ids = [embedded_ref.artifact_id for _, embedded_ref in embedded_refs]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise AssemblyCandidateBundleError(
            "executed candidate contains duplicate embedded artifact refs"
        )
    if ref.artifact_id in artifact_ids:
        raise AssemblyCandidateBundleError(
            "executed candidate bundle cannot reference itself"
        )
    for role, embedded_ref in embedded_refs[2:]:
        _resolve_embedded_ref(data_dir, session_id, embedded_ref, role=role)

    return VerifiedExecutedCandidate(
        ref=ref,
        bundle=bundle,
        path=bundle_path,
        plan_ref=plan.ref,
        plan=plan.bundle,
        plan_path=plan.path,
        primary_output_ref=primary_output_ref,
        primary_output_path=primary_output_path,
    )


def publish_registered_assembly_candidate(
    data_dir: Path | str,
    session_id: str,
    *,
    workflow_step_id: str,
    asset_id: str,
    source_path: Path | str,
    expected_sha256: str,
    expected_size_bytes: int,
    asset_registry_ref: ArtifactRef | dict[str, Any],
) -> VerifiedExecutedCandidate:
    """Bind one registered external assembly to the executed-candidate protocol."""

    source = Path(source_path).expanduser().resolve(strict=False)
    if not source.is_file():
        raise FileNotFoundError(source)
    if (
        source.stat().st_size != expected_size_bytes
        or sha256_file(source) != expected_sha256
    ):
        raise AssemblyCandidateBundleError(
            f"registered assembly asset bytes drifted: {asset_id!r}"
        )
    registry_ref = ArtifactRef.model_validate(asset_registry_ref)
    resolve_artifact_ref(data_dir, session_id, registry_ref)

    safe_asset_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in asset_id
    )
    if not safe_asset_id:
        raise AssemblyCandidateBundleError(
            "registered assembly asset has no usable identity"
        )
    paths = SessionPaths.from_run_dir(data_dir)
    method_name = "inventory.registered_assembly"
    candidate_id = f"{session_id}:registered_assembly:{asset_id}"
    candidate_dir = paths.workflow_step_artifact_path(
        workflow_step_id,
        "inventory",
        "registered_assembly",
        safe_asset_id,
    )
    work_dir = paths.workflow_step_work_path(
        workflow_step_id,
        "inventory",
        "registered_assembly",
        safe_asset_id,
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    planned = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=method_name,
        status="planned",
        input_type="assembly_fasta_registered",
        input_member_ids={"primary": [asset_id]},
        input_member_platforms={"primary": ["registered_external"]},
        parameter_attempt_id=0,
        expected_outputs=["primary_assembly"],
    )
    plan_destination = candidate_dir / "candidate.planned.json"
    plan_source = work_dir / f".candidate.planned.{uuid4().hex}.tmp"
    plan_source.write_text(
        json.dumps(
            planned.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    plan_artifact = ArtifactMeta.from_file(
        plan_source,
        stage="inventory",
        method_name=method_name,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "dependency_refs": [registry_ref.model_dump(mode="json")],
            "provenance": {"source_asset_id": asset_id},
        },
    )
    try:
        published_plan = publish_artifact_record(
            data_dir,
            session_id,
            plan_artifact,
            destination=plan_destination,
        )
    finally:
        plan_source.unlink(missing_ok=True)

    primary_extension = _registered_assembly_extension(source)
    primary_destination = candidate_dir / f"primary_assembly{primary_extension}"
    primary_source = work_dir / (
        f".primary_assembly.{uuid4().hex}{primary_extension}.tmp"
    )
    shutil.copy2(source, primary_source)
    primary_artifact = ArtifactMeta.from_file(
        primary_source,
        stage="inventory",
        method_name=method_name,
        artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        metadata={
            "candidate_id": candidate_id,
            "dependency_refs": [registry_ref.model_dump(mode="json")],
            "provenance": {"source_asset_id": asset_id},
        },
    )
    try:
        published_primary = publish_artifact_record(
            data_dir,
            session_id,
            primary_artifact,
            destination=primary_destination,
        )
    finally:
        primary_source.unlink(missing_ok=True)

    executed = planned.model_copy(
        update={
            "status": "executed",
            "plan_ref": published_plan.ref,
            "primary_output": published_primary.ref,
        }
    )
    executed_destination = candidate_dir / "candidate.executed.json"
    executed_source = work_dir / f".candidate.executed.{uuid4().hex}.tmp"
    executed_source.write_text(
        json.dumps(
            executed.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    executed_artifact = ArtifactMeta.from_file(
        executed_source,
        stage="inventory",
        method_name=method_name,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "dependency_refs": [
                published_plan.ref.model_dump(mode="json"),
                published_primary.ref.model_dump(mode="json"),
                registry_ref.model_dump(mode="json"),
            ],
            "provenance": {"source_asset_id": asset_id},
        },
    )
    try:
        published_executed = publish_artifact_record(
            data_dir,
            session_id,
            executed_artifact,
            destination=executed_destination,
        )
    finally:
        executed_source.unlink(missing_ok=True)

    return load_executed_candidate_bundle(
        data_dir,
        session_id,
        published_executed.ref,
        expected_method_name=method_name,
        expected_candidate_id=candidate_id,
        expected_parameter_attempt_id=0,
    )


def publish_derived_assembly_candidate(
    data_dir: Path | str,
    session_id: str,
    *,
    workflow_step_id: str,
    source_candidate: (VerifiedExecutedCandidate | ArtifactRef | dict[str, Any]),
    primary_output_ref: ArtifactRef | dict[str, Any],
    stage: str,
    method_name: str,
    action_identity: str,
    log_refs: dict[str, ArtifactRef | dict[str, Any]] | None = None,
) -> VerifiedExecutedCandidate:
    """Publish a new candidate chain for a materialized Assembly derivation."""

    if not stage or not method_name or not action_identity:
        raise AssemblyCandidateBundleError(
            "derived candidate requires stage, method, and action identity"
        )
    if len(stage) > 128 or len(method_name) > 256 or len(action_identity) > 512:
        raise AssemblyCandidateBundleError(
            "derived candidate action identity is not bounded"
        )
    source_ref = (
        source_candidate.ref
        if isinstance(source_candidate, VerifiedExecutedCandidate)
        else source_candidate
    )
    verified_source = load_executed_candidate_bundle(
        data_dir,
        session_id,
        source_ref,
    )
    derived_primary_ref = ArtifactRef.model_validate(primary_output_ref)
    if (
        derived_primary_ref.artifact_type
        not in CANONICAL_ASSEMBLY_CANDIDATE_PRIMARY_TYPES
    ):
        raise AssemblyCandidateBundleError(
            "derived candidate primary output has the wrong artifact type"
        )
    resolve_artifact_ref(data_dir, session_id, derived_primary_ref)
    if derived_primary_ref == verified_source.primary_output_ref:
        raise AssemblyCandidateBundleError(
            "derived candidate primary output must differ from its source"
        )

    normalized_logs = {
        role: ArtifactRef.model_validate(raw_ref)
        for role, raw_ref in (log_refs or {}).items()
    }
    if normalized_logs and set(normalized_logs) != {"stdout", "stderr"}:
        raise AssemblyCandidateBundleError(
            "derived candidate logs must be the canonical stdout/stderr pair"
        )
    for role, log_ref in normalized_logs.items():
        if not role:
            raise AssemblyCandidateBundleError(
                "derived candidate log role cannot be empty"
            )
        expected_type = f"command.{role}"
        if log_ref.artifact_type != expected_type:
            raise AssemblyCandidateBundleError(
                f"derived candidate {role} has artifact type "
                f"{log_ref.artifact_type!r}, expected {expected_type!r}"
            )
        resolve_artifact_ref(data_dir, session_id, log_ref)

    identity_payload = json.dumps(
        [
            verified_source.ref.artifact_id,
            stage,
            method_name,
            action_identity,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity_digest = sha256(identity_payload.encode("utf-8")).hexdigest()
    candidate_id = (
        f"{verified_source.bundle.candidate_id}:derived:{identity_digest[:16]}"
    )
    planned = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=method_name,
        status="planned",
        input_type=verified_source.bundle.input_type,
        input_member_ids={
            role: list(member_ids)
            for role, member_ids in verified_source.bundle.input_member_ids.items()
        },
        input_member_platforms={
            role: list(platforms)
            for role, platforms in (
                verified_source.bundle.input_member_platforms.items()
            )
        },
        parameter_attempt_id=0,
        genome_size_prior=verified_source.bundle.genome_size_prior,
        expected_outputs=["primary_assembly"],
    )

    paths = SessionPaths.from_run_dir(data_dir)
    safe_method = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in method_name
    ).strip("_")
    candidate_dir = paths.workflow_step_artifact_path(
        workflow_step_id,
        stage,
        "derived_candidates",
        f"{safe_method[:64]}-{identity_digest[:16]}",
    )
    work_dir = paths.workflow_step_work_path(
        workflow_step_id,
        stage,
        "derived_candidates",
        identity_digest[:16],
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    source_dependencies = [
        verified_source.ref.model_dump(mode="json"),
        verified_source.primary_output_ref.model_dump(mode="json"),
    ]
    plan_source = work_dir / f".candidate.planned.{uuid4().hex}.tmp"
    plan_source.write_text(
        json.dumps(
            planned.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    plan_artifact = ArtifactMeta.from_file(
        plan_source,
        stage=stage,
        method_name=method_name,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "producer_event_id": action_identity,
            "dependency_refs": source_dependencies,
        },
    )
    try:
        published_plan = publish_artifact_record(
            data_dir,
            session_id,
            plan_artifact,
            destination=candidate_dir / "candidate.planned.json",
        )
    finally:
        plan_source.unlink(missing_ok=True)

    executed_payload = planned.model_dump(mode="json")
    executed_payload.update(
        {
            "status": "executed",
            "plan_ref": published_plan.ref.model_dump(mode="json"),
            "primary_output": derived_primary_ref.model_dump(mode="json"),
            "log_refs": {
                role: log_ref.model_dump(mode="json")
                for role, log_ref in normalized_logs.items()
            },
        }
    )
    executed = AssemblyCandidateBundle.model_validate(executed_payload)
    executed_source = work_dir / f".candidate.executed.{uuid4().hex}.tmp"
    executed_source.write_text(
        json.dumps(
            executed.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    executed_artifact = ArtifactMeta.from_file(
        executed_source,
        stage=stage,
        method_name=method_name,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "producer_event_id": action_identity,
            "dependency_refs": [
                published_plan.ref.model_dump(mode="json"),
                derived_primary_ref.model_dump(mode="json"),
                *source_dependencies,
                *[
                    log_ref.model_dump(mode="json")
                    for log_ref in normalized_logs.values()
                ],
            ],
        },
    )
    try:
        published_executed = publish_artifact_record(
            data_dir,
            session_id,
            executed_artifact,
            destination=candidate_dir / "candidate.executed.json",
        )
    finally:
        executed_source.unlink(missing_ok=True)

    verified_derived = load_executed_candidate_bundle(
        data_dir,
        session_id,
        published_executed.ref,
        expected_method_name=method_name,
        expected_candidate_id=candidate_id,
        expected_parameter_attempt_id=0,
    )
    if verified_derived.primary_output_ref != derived_primary_ref:
        raise AssemblyCandidateBundleError(
            "published derived candidate primary output ref drifted"
        )
    return verified_derived


def _load_candidate_bundle(
    data_dir: Path | str,
    session_id: str,
    value: ArtifactRef | dict[str, Any],
    *,
    expected_status: str,
) -> tuple[ArtifactRef, Path, AssemblyCandidateBundle]:
    try:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
    except (TypeError, ValidationError) as exc:
        raise AssemblyCandidateBundleError(
            "candidate bundle handoff is not a valid ArtifactRef"
        ) from exc
    if ref.artifact_type != ASSEMBLY_CANDIDATE_BUNDLE:
        raise AssemblyCandidateBundleError(
            f"candidate bundle ref has artifact type {ref.artifact_type!r}, "
            f"expected {ASSEMBLY_CANDIDATE_BUNDLE!r}"
        )
    try:
        path = resolve_artifact_ref(data_dir, session_id, ref)
    except (OSError, TypeError, ValueError) as exc:
        raise AssemblyCandidateBundleError(
            f"candidate bundle ref failed index/integrity validation: {ref.artifact_id}"
        ) from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        bundle = AssemblyCandidateBundle.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise AssemblyCandidateBundleError(
            f"candidate bundle payload is invalid: {ref.artifact_id}"
        ) from exc
    if bundle.session_id != session_id:
        raise AssemblyCandidateBundleError(
            f"candidate bundle session mismatch: {bundle.session_id!r} != "
            f"{session_id!r}"
        )
    if bundle.status != expected_status:
        raise AssemblyCandidateBundleError(
            f"candidate bundle status is {bundle.status!r}, expected "
            f"{expected_status!r}"
        )
    return ref, path, bundle


def _validate_expected_identity(
    bundle: AssemblyCandidateBundle,
    *,
    expected_method_name: str | None,
    expected_candidate_id: str | None,
    expected_parameter_attempt_id: int | None,
) -> None:
    expected_values = (
        ("method_name", expected_method_name),
        ("candidate_id", expected_candidate_id),
        ("parameter_attempt_id", expected_parameter_attempt_id),
    )
    for field, expected in expected_values:
        if expected is not None and getattr(bundle, field) != expected:
            raise AssemblyCandidateBundleError(
                f"candidate {field} mismatch: {getattr(bundle, field)!r} != "
                f"{expected!r}"
            )


def _validate_plan_identity(
    executed: AssemblyCandidateBundle,
    planned: AssemblyCandidateBundle,
) -> None:
    identity_fields = (
        "session_id",
        "candidate_id",
        "method_name",
        "artifact_type",
        "input_type",
        "input_member_ids",
        "input_member_platforms",
        "parameter_attempt_id",
        "genome_size_prior",
        "expected_outputs",
    )
    for field in identity_fields:
        if getattr(executed, field) != getattr(planned, field):
            raise AssemblyCandidateBundleError(
                f"executed candidate changed planned identity field {field!r}"
            )


def _resolve_embedded_ref(
    data_dir: Path | str,
    session_id: str,
    ref: ArtifactRef,
    *,
    role: str,
) -> Path:
    try:
        return resolve_artifact_ref(data_dir, session_id, ref)
    except (OSError, TypeError, ValueError) as exc:
        raise AssemblyCandidateBundleError(
            f"candidate {role} ref failed index/integrity validation: {ref.artifact_id}"
        ) from exc


def _registered_assembly_extension(path: Path) -> str:
    suffixes = path.suffixes
    if not suffixes:
        return ".fasta"
    if suffixes[-1].lower() in {".gz", ".bgz"} and len(suffixes) >= 2:
        extension = "".join(suffixes[-2:])
    else:
        extension = suffixes[-1]
    if not all(
        character.isalnum() or character in {".", "_", "-"} for character in extension
    ):
        raise AssemblyCandidateBundleError(
            f"registered assembly has an unsafe filename extension: {extension!r}"
        )
    return extension
