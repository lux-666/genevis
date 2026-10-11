"""T2T motif resolution and deterministic baseline evidence workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.domains.parameter_recommendation.resolution import (
    render_stage_values,
    resolve_parameters,
)
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    update_current_parameter_attempt,
)
from app.llm.parameters.base import PARAMETER_RECOMMENDATION_CONTRACT
from app.llm.parameters.t2t.motif import (
    MOTIF_RESOLUTION_CONTRACT,
    build_t2t_motif_request,
    render_t2t_motif_messages,
    restore_t2t_motif_decision,
    t2t_motif_prompt_hash,
)
from app.llm.parameters.t2t.motif import (
    PROMPT_BUNDLE as MOTIF_PROMPT_BUNDLE,
)
from app.llm.parameters.t2t.motif import (
    resolve_t2t_motif as invoke_t2t_motif_model,
)
from app.review.approval import (
    persist_parameter_approval_decision,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    persist_review_request,
)
from app.schemas.artifacts import (
    T2T_MOTIF_INPUT_SNAPSHOT,
    T2T_MOTIF_MODEL_REQUEST,
    T2T_MOTIF_MODEL_RESPONSE,
    T2T_MOTIF_RESOLUTION,
    T2T_MOTIF_RESOLUTION_MANIFEST,
    T2T_MOTIF_RESPONSE_PARSE,
    T2T_PARAMETER_APPROVAL_DECISION,
    T2T_PARAMETER_BASIS,
    T2T_PARAMETER_FROZEN_PARAMETERS,
    T2T_PARAMETER_INPUT_SNAPSHOT,
    T2T_PARAMETER_POLICY_REQUEST,
    T2T_PARAMETER_POLICY_RESPONSE,
    T2T_PARAMETER_RECOMMENDATION,
    T2T_PARAMETER_RECOMMENDATION_MANIFEST,
    T2T_PARAMETER_RESPONSE_PARSE,
    ArtifactRef,
    ArtifactRefJson,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.parameter_recommendation import (
    T2T_MOTIF_RESOLVER_ID,
    CanonicalRecommendationFacts,
    CanonicalSpeciesFacts,
    ParameterRecommendationManifestV3,
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
    T2TMotifInputSnapshotV3,
    T2TMotifResolutionManifestV2,
    T2TRecommendationContext,
)
from app.schemas.shared_parameters import DnaMotif, normalize_dna_motif
from app.schemas.state import StateUpdate, TopLevelState
from app.schemas.t2t import T2TThresholds
from app.stages.t2t.parameters.catalog import T2T_METHOD, load_t2t_parameter_catalog
from app.stages.t2t.parameters.context import build_t2t_recommendation_context
from app.stages.t2t.parameters.execution import (
    T2TFrozenExecutionInputs,
    load_chromosome_names_snapshot,
    load_t2t_baseline_bundle,
)
from app.stages.t2t.parameters.policy import baseline_request, baseline_response
from app.stages.t2t.parameters.storage import (
    ParameterPersistenceError,
)
from app.stages.t2t.parameters.storage import (
    read_artifact as read_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.storage import (
    recover_artifact as recover_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.storage import (
    write_artifact as write_t2t_parameter_artifact,
)
from app.stages.t2t.state import (
    read_t2t_stage_output,
    update_t2t_stage_output,
)
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    build_recommendation_dependency_refs,
    canonical_parameter_dependency,
)

_RECOMMENDER_METHOD = "t2t.parameter_recommender"
_APPROVAL_METHOD = "t2t.parameter_approval"
_MOTIF_RESOLVER_METHOD = T2T_MOTIF_RESOLVER_ID


def resolve_t2t_telomere_motif(state: TopLevelState) -> StateUpdate:
    """Resolve one motif before any T2T recommendation or sequence scan."""

    output = read_t2t_stage_output(state)
    if output.get("status") != "resolved":
        return {}
    if output.get("recommendation_mode") not in {"advisor", "deterministic_baseline"}:
        return _fail(
            state, "T2T requires explicit advisor or deterministic_baseline mode"
        )
    existing_manifest_ref = output.get("telomere_motif_resolution_manifest_ref")
    if existing_manifest_ref is not None:
        try:
            hydrated, _snapshot = _hydrate_t2t_motif_resolution(
                state, existing_manifest_ref
            )
            return hydrated
        except (ValueError, ParameterPersistenceError) as exc:
            return _fail(state, f"T2T motif evidence is missing or changed: {exc}")

    shared = state.get("shared_parameters")
    if shared is None:
        shared = {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(mode="json", exclude_none=True)
    try:
        canonical_facts = build_canonical_facts(
            shared_parameters=shared,
            read_qc_output=None,
            genome_survey_result=None,
        )
    except ValueError as exc:
        return _fail(state, f"T2T canonical species facts are invalid: {exc}")
    species_facts: CanonicalSpeciesFacts = canonical_facts.species
    snapshot = T2TMotifInputSnapshotV3(
        schema_version=3,
        facts=species_facts,
        dependency_refs=RecommendationDependencyRefs(),
    )
    try:
        input_snapshot_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_input_snapshot.json",
            snapshot.model_dump(mode="json"),
            artifact_type=T2T_MOTIF_INPUT_SNAPSHOT,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif snapshot persistence failed: {exc}")

    if (
        output.get("recommendation_mode") == "deterministic_baseline"
        and not species_facts.supplied_telomere_motif
    ):
        return _fail(
            state, "T2T deterministic_baseline requires a supplied telomere motif"
        )

    if species_facts.supplied_telomere_motif:
        try:
            motif = normalize_dna_motif(
                species_facts.supplied_telomere_motif,
                field_name="species_facts.supplied_telomere_motif",
            )
        except ValueError as exc:
            return _fail(state, f"supplied telomere motif is invalid: {exc}")
        summary = "Motif supplied directly in species facts; no LLM call required."
        try:
            resolution_ref, manifest_ref = _write_t2t_motif_resolution_chain(
                state,
                input_snapshot_ref=input_snapshot_ref,
                resolution_mode="provided_species_fact",
                status="resolved",
                motif=motif,
                summary=summary,
            )
        except (OSError, ValueError, ParameterPersistenceError) as exc:
            return _fail(state, f"T2T motif resolution persistence failed: {exc}")
        return update_t2t_stage_output(
            state,
            {
                "status": "motif_resolved",
                "resolved_telomere_motif": motif,
                "resolved_telomere_motif_source": "provided_species_fact",
                "resolved_telomere_motif_summary": summary,
                "telomere_motif_input_snapshot_ref": input_snapshot_ref.model_dump(
                    mode="json"
                ),
                "telomere_motif_resolution_ref": resolution_ref.model_dump(mode="json"),
                "telomere_motif_resolution_manifest_ref": manifest_ref.model_dump(
                    mode="json"
                ),
            },
        )

    try:
        request = build_t2t_motif_request(facts=species_facts)
        messages = render_t2t_motif_messages(request)
        request_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_model_request.json",
            {
                "schema_version": 3,
                "prompt_bundle_id": MOTIF_PROMPT_BUNDLE,
                "prompt_template_sha256": t2t_motif_prompt_hash(),
                "messages": messages,
            },
            artifact_type=T2T_MOTIF_MODEL_REQUEST,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif request failed: {exc}")

    response_ref: ArtifactRef | None = None
    try:
        recovered = recover_t2t_parameter_artifact(
            state,
            0,
            "motif_model_response.json",
            expected_type=T2T_MOTIF_MODEL_RESPONSE,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
        if recovered is None:
            decision = invoke_t2t_motif_model(request, messages=messages)
        else:
            response_ref, record = recovered
            decision = restore_t2t_motif_decision(record)
        if response_ref is None:
            response_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_model_response.json",
                {
                    "schema_version": 2,
                    "raw_response": decision.raw_response,
                    "validity": "parsed" if decision.valid else "invalid",
                    "error": decision.error,
                    "model_id": decision.model_id,
                },
                artifact_type=T2T_MOTIF_MODEL_RESPONSE,
                method_name=_MOTIF_RESOLVER_METHOD,
            )
        parse_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_response_parse.json",
            {
                "schema_version": 1,
                "parsed": decision.valid,
                "error": decision.error,
            },
            artifact_type=T2T_MOTIF_RESPONSE_PARSE,
            method_name="t2t.telomere_motif_response_parser",
        )
        if decision.valid and decision.status is None:
            raise ValueError("T2T motif response has no resolution status")
        resolution_status: Literal["resolved", "unresolved", "invalid"] = (
            decision.status if decision.status is not None else "invalid"
        )
        resolution_ref, manifest_ref = _write_t2t_motif_resolution_chain(
            state,
            input_snapshot_ref=input_snapshot_ref,
            resolution_mode="llm_species_resolution",
            status=resolution_status,
            motif=decision.resolved_telomere_motif,
            summary=decision.summary,
            model_request_ref=request_ref,
            model_response_ref=response_ref,
            response_parse_ref=parse_ref,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif response persistence failed: {exc}")

    refs = {
        "telomere_motif_input_snapshot_ref": input_snapshot_ref.model_dump(mode="json"),
        "telomere_motif_model_request_ref": request_ref.model_dump(mode="json"),
        "telomere_motif_model_response_ref": response_ref.model_dump(mode="json"),
        "telomere_motif_response_parse_ref": parse_ref.model_dump(mode="json"),
        "telomere_motif_resolution_ref": resolution_ref.model_dump(mode="json"),
        "telomere_motif_resolution_manifest_ref": manifest_ref.model_dump(mode="json"),
    }
    if not decision.valid:
        return _fail_with_motif_refs(
            state,
            "T2T telomere motif response could not be parsed",
            refs,
        )
    if decision.status != "resolved" or not decision.resolved_telomere_motif:
        return _fail_with_motif_refs(
            state,
            "T2T telomere motif is unresolved for the supplied species facts",
            refs,
        )
    return update_t2t_stage_output(
        state,
        {
            "status": "motif_resolved",
            "resolved_telomere_motif": decision.resolved_telomere_motif,
            "resolved_telomere_motif_source": "llm_species_resolution",
            "resolved_telomere_motif_summary": decision.summary,
            **refs,
        },
    )


def _write_t2t_motif_resolution_chain(
    state: TopLevelState,
    *,
    input_snapshot_ref: ArtifactRef,
    resolution_mode: Literal["provided_species_fact", "llm_species_resolution"],
    status: Literal["resolved", "unresolved", "invalid"],
    motif: DnaMotif | None,
    summary: str | None,
    model_request_ref: ArtifactRef | None = None,
    model_response_ref: ArtifactRef | None = None,
    response_parse_ref: ArtifactRef | None = None,
) -> tuple[ArtifactRef, ArtifactRef]:
    resolution_ref = write_t2t_parameter_artifact(
        state,
        0,
        "motif_resolution.json",
        {
            "schema_version": 1,
            "resolution_contract": MOTIF_RESOLUTION_CONTRACT,
            "status": status,
            "resolved_telomere_motif": motif,
            "source": resolution_mode,
            "summary": summary,
        },
        artifact_type=T2T_MOTIF_RESOLUTION,
        method_name=_MOTIF_RESOLVER_METHOD,
    )
    manifest = T2TMotifResolutionManifestV2.model_validate(
        {
            "schema_version": 2,
            "resolution_contract": MOTIF_RESOLUTION_CONTRACT,
            "resolver_id": T2T_MOTIF_RESOLVER_ID,
            "resolution_mode": resolution_mode,
            "status": status,
            "refs": {
                "input_snapshot_ref": input_snapshot_ref,
                "resolution_ref": resolution_ref,
                "model_request_ref": model_request_ref,
                "model_response_ref": model_response_ref,
                "response_parse_ref": response_parse_ref,
            },
        }
    )
    manifest_ref = write_t2t_parameter_artifact(
        state,
        0,
        "motif_resolution_manifest.json",
        manifest.model_dump(mode="json", exclude_none=True),
        artifact_type=T2T_MOTIF_RESOLUTION_MANIFEST,
        method_name=_MOTIF_RESOLVER_METHOD,
    )
    return resolution_ref, manifest_ref


def _hydrate_t2t_motif_resolution(
    state: TopLevelState,
    manifest_value: ArtifactRef | ArtifactRefJson,
) -> tuple[StateUpdate, T2TMotifInputSnapshotV3]:
    manifest_record = read_t2t_parameter_artifact(
        state,
        manifest_value,
        expected_type=T2T_MOTIF_RESOLUTION_MANIFEST,
    )
    if manifest_record.get("schema_version") != 2:
        raise ValueError("unsupported T2T motif resolution manifest schema")
    manifest = T2TMotifResolutionManifestV2.model_validate(manifest_record)
    snapshot_record = read_t2t_parameter_artifact(
        state,
        manifest.refs.input_snapshot_ref,
        expected_type=T2T_MOTIF_INPUT_SNAPSHOT,
    )
    if snapshot_record.get("schema_version") != 3:
        raise ValueError("unsupported T2T motif input snapshot schema")
    snapshot = T2TMotifInputSnapshotV3.model_validate(snapshot_record)
    resolution_record = read_t2t_parameter_artifact(
        state,
        manifest.refs.resolution_ref,
        expected_type=T2T_MOTIF_RESOLUTION,
    )
    resolution = _validate_t2t_motif_resolution_record(
        resolution_record,
        expected_mode=manifest.resolution_mode,
        expected_status=manifest.status,
    )
    if (
        read_t2t_stage_output(state).get("recommendation_mode")
        == "deterministic_baseline"
        and manifest.resolution_mode != "provided_species_fact"
    ):
        raise ValueError(
            "T2T deterministic_baseline requires a supplied telomere motif"
        )
    if manifest.resolution_mode == "provided_species_fact":
        supplied = snapshot.facts.supplied_telomere_motif
        if (
            supplied is None
            or normalize_dna_motif(
                supplied,
                field_name="motif_snapshot.facts.supplied_telomere_motif",
            )
            != resolution["resolved_telomere_motif"]
        ):
            raise ValueError(
                "provided T2T supplied motif does not match its input snapshot"
            )
    input_snapshot_ref_json = manifest.refs.input_snapshot_ref.model_dump(mode="json")
    refs = {
        "telomere_motif_input_snapshot_ref": input_snapshot_ref_json,
        "telomere_motif_resolution_ref": manifest.refs.resolution_ref.model_dump(
            mode="json"
        ),
        "telomere_motif_resolution_manifest_ref": ArtifactRef.model_validate(
            manifest_value
        ).model_dump(mode="json"),
    }
    if manifest.resolution_mode == "llm_species_resolution":
        model_refs = {
            "telomere_motif_model_request_ref": manifest.refs.model_request_ref,
            "telomere_motif_model_response_ref": manifest.refs.model_response_ref,
            "telomere_motif_response_parse_ref": manifest.refs.response_parse_ref,
        }
        if (
            manifest.refs.model_request_ref is None
            or manifest.refs.model_response_ref is None
            or manifest.refs.response_parse_ref is None
        ):
            raise ValueError("T2T motif manifest is missing required model refs")
        request_record = read_t2t_parameter_artifact(
            state,
            manifest.refs.model_request_ref,
            expected_type=T2T_MOTIF_MODEL_REQUEST,
        )
        if request_record.get("schema_version") != 3:
            raise ValueError("unsupported T2T motif model request schema")
        response_record = read_t2t_parameter_artifact(
            state,
            manifest.refs.model_response_ref,
            expected_type=T2T_MOTIF_MODEL_RESPONSE,
        )
        decision = restore_t2t_motif_decision(response_record)
        parse_record = read_t2t_parameter_artifact(
            state,
            manifest.refs.response_parse_ref,
            expected_type=T2T_MOTIF_RESPONSE_PARSE,
        )
        if parse_record.get("schema_version") != 1:
            raise ValueError("unsupported T2T motif response parse schema")
        expected_status = decision.status if decision.valid else "invalid"
        if (
            expected_status != manifest.status
            or decision.resolved_telomere_motif != resolution["resolved_telomere_motif"]
            or decision.summary != resolution["summary"]
        ):
            raise ValueError("T2T motif model records disagree with the resolution")
        refs.update(
            {
                key: ref.model_dump(mode="json")
                for key, ref in model_refs.items()
                if ref is not None
            }
        )
    if manifest.status != "resolved" or not resolution["resolved_telomere_motif"]:
        return (
            _fail_with_motif_refs(
                state,
                "T2T telomere motif evidence does not contain a resolved motif",
                refs,
            ),
            snapshot,
        )
    return (
        update_t2t_stage_output(
            state,
            {
                "status": "motif_resolved",
                "resolved_telomere_motif": resolution["resolved_telomere_motif"],
                "resolved_telomere_motif_source": manifest.resolution_mode,
                "resolved_telomere_motif_summary": resolution["summary"],
                **refs,
            },
        ),
        snapshot,
    )


def _validate_t2t_motif_resolution_record(
    record: dict[str, Any],
    *,
    expected_mode: str,
    expected_status: str,
) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "resolution_contract",
        "status",
        "resolved_telomere_motif",
        "source",
        "summary",
    }
    if set(record) != expected_fields:
        raise ValueError("T2T motif resolution record has an unsupported shape")
    if (
        record.get("schema_version") != 1
        or record.get("resolution_contract") != MOTIF_RESOLUTION_CONTRACT
        or record.get("source") != expected_mode
        or record.get("status") != expected_status
    ):
        raise ValueError("T2T motif resolution record identity mismatch")
    motif = record.get("resolved_telomere_motif")
    if expected_status == "resolved":
        if not isinstance(motif, str) or normalize_dna_motif(motif) != motif:
            raise ValueError("T2T motif resolution record has an invalid motif")
    elif motif is not None:
        raise ValueError("unresolved T2T motif resolution must not contain a motif")
    summary = record.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("T2T motif resolution summary must be a string or null")
    return dict(record)


def _fail_with_motif_refs(
    state: TopLevelState,
    reason: str,
    refs: Mapping[str, object],
) -> StateUpdate:
    return update_t2t_stage_output(
        state,
        {"status": "failed", "reason": reason, **refs},
    )


def recommend_t2t_parameters(state: TopLevelState) -> StateUpdate:
    """Persist and approve the empty-L2 deterministic baseline policy."""

    output = read_t2t_stage_output(state)
    if output.get("status") != "motif_resolved":
        return {}
    if output.get("recommendation_mode") != "deterministic_baseline":
        return _fail(state, "T2T schema-3 parameters require deterministic_baseline")
    read_inputs = output.get("long_read_inputs")
    if not isinstance(read_inputs, list) or not read_inputs:
        return {}
    motif_manifest_ref = output.get("telomere_motif_resolution_manifest_ref")
    if motif_manifest_ref is None:
        return _fail(state, "T2T threshold recommendation requires motif evidence")
    try:
        hydrated_update, motif_snapshot = _hydrate_t2t_motif_resolution(
            state, motif_manifest_ref
        )
    except (ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif evidence is missing or changed: {exc}")
    hydrated_state: TopLevelState = {**state, **hydrated_update}
    hydrated_output = read_t2t_stage_output(hydrated_state)
    if hydrated_output.get("status") != "motif_resolved":
        return hydrated_update
    bound_fields = (
        "resolved_telomere_motif",
        "resolved_telomere_motif_source",
        "resolved_telomere_motif_summary",
        "telomere_motif_input_snapshot_ref",
        "telomere_motif_resolution_ref",
        "telomere_motif_resolution_manifest_ref",
    )
    if any(output.get(field) != hydrated_output.get(field) for field in bound_fields):
        return _fail(state, "T2T motif checkpoint does not match its evidence chain")
    output = hydrated_output
    try:
        facts = CanonicalRecommendationFacts(
            species=motif_snapshot.facts,
        )
        context = build_t2t_recommendation_context(state, t2t_out=output)
        registry_ref = canonical_parameter_dependency(
            state, state.get("asset_registry_ref")
        )
        assembly_ref = canonical_parameter_dependency(
            state, output.get("input_assembly_ref")
        )
        stage_input_paths = (
            {
                "assembly": assembly_ref,
                "asset_registry": registry_ref,
            }
            if registry_ref is not None
            else {
                "assembly": assembly_ref,
                **{
                    f"long_read_{index:04d}": _t2t_long_read_input_path(
                        item, index=index
                    )
                    for index, item in enumerate(read_inputs)
                },
            }
        )
        dependency_refs = build_recommendation_dependency_refs(
            state,
            stage_input_paths=stage_input_paths,
            motif_resolution_ref=output.get("telomere_motif_resolution_ref"),
        )
        catalog = load_t2t_parameter_catalog()
        input_snapshot_ref = write_t2t_parameter_artifact(
            state,
            0,
            "input_snapshot.json",
            RecommendationInputSnapshotV3(
                schema_version=3,
                method_id=catalog.method_id,
                task="recommend_initial",
                facts=facts,
                stage_context=context,
                dependency_refs=dependency_refs,
            ).model_dump(mode="json"),
            artifact_type=T2T_PARAMETER_INPUT_SNAPSHOT,
            method_name=_RECOMMENDER_METHOD,
        )
        basis_ref = write_t2t_parameter_artifact(
            state,
            0,
            "basis_snapshot.json",
            {
                "schema_version": 3,
                "method_id": catalog.method_id,
                "catalog_snapshot": catalog.model_dump(mode="json"),
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
            },
            artifact_type=T2T_PARAMETER_BASIS,
            method_name=_RECOMMENDER_METHOD,
        )
        request_ref = write_t2t_parameter_artifact(
            state,
            0,
            "policy_request.json",
            baseline_request(input_snapshot_ref, basis_ref),
            artifact_type=T2T_PARAMETER_POLICY_REQUEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T parameter request failed: {exc}")

    response_ref: ArtifactRef | None = None
    try:
        recovered = recover_t2t_parameter_artifact(
            state,
            0,
            "policy_response.json",
            expected_type=T2T_PARAMETER_POLICY_RESPONSE,
            method_name=_RECOMMENDER_METHOD,
        )
        policy_record = baseline_response()
        if recovered is not None:
            response_ref, record = recovered
            if record != policy_record:
                return _fail(state, "T2T baseline response does not match its policy")
        if response_ref is None:
            response_ref = write_t2t_parameter_artifact(
                state,
                0,
                "policy_response.json",
                policy_record,
                artifact_type=T2T_PARAMETER_POLICY_RESPONSE,
                method_name=_RECOMMENDER_METHOD,
            )
        recommendation = {
            "parameters": policy_record["parameters"],
            "summary": policy_record["summary"],
        }
        recommendation_ref = write_t2t_parameter_artifact(
            state,
            0,
            "recommendation.json",
            {
                "schema_version": 2,
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "recommendation": recommendation,
            },
            artifact_type=T2T_PARAMETER_RECOMMENDATION,
            method_name=_RECOMMENDER_METHOD,
        )
        parse_ref = write_t2t_parameter_artifact(
            state,
            0,
            "response_parse.json",
            {"schema_version": 1, "parsed": True, "error": None},
            artifact_type=T2T_PARAMETER_RESPONSE_PARSE,
            method_name="t2t.parameter_response_parser",
        )
        manifest = ParameterRecommendationManifestV3.model_validate(
            {
                "schema_version": 3,
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
                "stage": "t2t",
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "recommendation_id": 0,
                "status": "pending_approval",
                "previous_attempt_id": None,
                "previous_attempt_manifest_ref": None,
                "previous_attempt_seal_hash": None,
                "refs": {
                    "input_snapshot_ref": input_snapshot_ref,
                    "basis_snapshot_ref": basis_ref,
                    "model_request_ref": request_ref,
                    "model_response_ref": response_ref,
                    "recommendation_ref": recommendation_ref,
                    "response_parse_ref": parse_ref,
                },
                "summary": {
                    "response_parsed": True,
                    "policy": "deterministic_baseline",
                    "model_invoked": False,
                },
            }
        )
        manifest_ref = write_t2t_parameter_artifact(
            state,
            0,
            "recommendation_manifest.json",
            manifest.model_dump(mode="json"),
            artifact_type=T2T_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail(state, f"T2T parameter response persistence failed: {exc}")

    cursor = ParameterRecommendationCursor(
        schema_version=3,
        recommendation_id=0,
        method_id=T2T_METHOD,
        task="recommend_initial",
        status="pending_approval",
        manifest_ref=manifest_ref,
        recommendation_hash=manifest_ref.sha256,
    )
    bindings = {
        "attempt_id": 0,
        "task": "recommend_initial",
        "previous_attempt_id": None,
        "recommendation_hash": manifest_ref.sha256,
    }
    try:
        persisted = persist_review_request(
            state,
            stage="t2t",
            gate_id="parameter_approval",
            reason="t2t_baseline_policy_approval",
            payload={
                "schema_version": 2,
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "recommendation_manifest_hash": manifest_ref.sha256,
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "resolved_telomere_motif": output["resolved_telomere_motif"],
                "resolved_telomere_motif_source": output.get(
                    "resolved_telomere_motif_source"
                ),
                "parameters": dict(recommendation.get("parameters") or {}),
                "rationale": recommendation.get("summary"),
                "response_parse_ref": parse_ref.model_dump(mode="json"),
                "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                "decisions_allowed": ["approve_parameters"],
                "auto_decidable": False,
            },
            method_name=_APPROVAL_METHOD,
            artifact_type="t2t.parameter_approval.request",
            bindings=bindings,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        return _fail(state, f"T2T parameter approval request failed: {exc}")
    cursor = cursor.model_copy(
        update={
            "review_request_id": persisted.review_request_id,
            "review_request_ref": persisted.request_ref,
        }
    )
    decision_ref = persist_parameter_approval_decision(
        state,
        stage="t2t",
        request_id=persisted.review_request_id,
        request_ref=persisted.request_ref,
        request=ResumeReviewRequest(
            session_id=state["session_id"],
            stage="t2t",
            gate_id="parameter_approval",
            review_request_id=persisted.review_request_id,
            review_request_hash=persisted.request_hash,
            decision="approve_parameters",
            decided_by="auto_rule",
            notes=["approved by deterministic_baseline policy"],
            rationale_artifact_ref=recommendation_ref,
            metadata={
                **bindings,
                "source": "policy",
                "configured_seconds": 0,
                "elapsed_seconds": 0,
                "automatic_resolution": {
                    "source": "policy",
                    "configured_seconds": 0,
                    "elapsed_seconds": 0.0,
                    "outcome": "approve_persisted_recommendation",
                },
            },
        ),
        manifest_ref=manifest_ref,
        method_name=_APPROVAL_METHOD,
        artifact_type=T2T_PARAMETER_APPROVAL_DECISION,
    )
    attempt = ParameterAttempt(
        schema_version=3,
        attempt_id=0,
        method_id=T2T_METHOD,
        task="recommend_initial",
        status="recommended",
        previous_attempt_id=None,
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=decision_ref,
        recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
    )
    return update_t2t_stage_output(
        state,
        {
            "status": "parameters_approved",
            "active_interrupt": None,
            "current_parameter_attempt_id": 0,
            "proposed_parameter_attempt_id": None,
            "parameter_attempts": [attempt.model_dump(mode="json")],
            "current_parameter_recommendation": cursor.model_copy(
                update={"status": "approved"}
            ).model_dump(mode="json"),
        },
    )


def freeze_t2t_parameters(state: TopLevelState) -> StateUpdate:
    """Freeze approved thresholds from the immutable v3 snapshot and catalog."""

    output = read_t2t_stage_output(state)
    if output.get("status") != "parameters_approved":
        return {}
    try:
        attempt = current_parameter_attempt(output, method_id=T2T_METHOD, label="T2T")
        bundle = load_t2t_baseline_bundle(state, output=output)
        resolved = resolve_parameters(
            bundle.chain.catalog,
            l0_executor_values=(
                {"t2t_mode": output["mode"]} if output.get("mode") is not None else {}
            ),
            snapshot=bundle.chain.input_snapshot,
            approved_l2=bundle.approved_l2,
        )
        stage_values = render_stage_values(resolved, bundle.chain.catalog)
        mode = stage_values.pop("mode", None)
        effective = T2TThresholds.model_validate(stage_values)
        context = bundle.chain.input_snapshot.stage_context
        if not isinstance(context, T2TRecommendationContext):
            raise ValueError("approved snapshot does not contain T2T context")
        execution_inputs = T2TFrozenExecutionInputs.model_validate(
            {
                "thresholds": effective.model_dump(mode="json"),
                "resolved_telomere_motif": context.resolved_telomere_motif,
                "motif_source": context.motif_source,
                "mode": mode,
                "input_assembly_ref": output.get("input_assembly_ref"),
                "long_read_inputs": output.get("long_read_inputs") or [],
                "chromosome_list_ref": output.get("chromosome_list_ref"),
                "chromosome_names": load_chromosome_names_snapshot(
                    output.get("chromosome_list_ref")
                ),
                "work_dir": output.get("work_dir"),
                "execution_enabled": output.get("execution_enabled"),
            }
        )
        effective_json = effective.model_dump(mode="json")
        recommendation_manifest_ref = attempt.recommendation_manifest_ref.model_dump(
            mode="json"
        )
        approval_decision_ref = attempt.approval_decision_ref.model_dump(mode="json")
        frozen_ref = write_t2t_parameter_artifact(
            state,
            attempt.attempt_id,
            "frozen_parameters.json",
            {
                "schema_version": 3,
                "stage": "t2t",
                "method_id": T2T_METHOD,
                **resolved.model_dump(mode="json"),
                "effective_thresholds": effective_json,
                "execution_inputs": execution_inputs.model_dump(mode="json"),
                "recommendation_manifest_ref": recommendation_manifest_ref,
                "approval_decision_ref": approval_decision_ref,
            },
            artifact_type=T2T_PARAMETER_FROZEN_PARAMETERS,
            method_name=T2T_METHOD,
            scope="attempt",
        )
        attempts = update_current_parameter_attempt(
            output,
            method_id=T2T_METHOD,
            label="T2T",
            frozen_parameters_ref=frozen_ref.model_dump(mode="json"),
        )
    except (
        ValueError,
        ParameterAttemptStateError,
        ParameterEvidenceError,
        ParameterPersistenceError,
    ) as exc:
        return _fail(state, f"T2T approved parameters are not executable: {exc}")
    return update_t2t_stage_output(
        state,
        {
            "status": "parameters_ready",
            "thresholds": effective_json,
            "resolved_telomere_motif": context.resolved_telomere_motif,
            "resolved_telomere_motif_source": context.motif_source,
            "mode": execution_inputs.mode,
            "parameter_attempts": attempts,
        },
    )


def _t2t_long_read_input_path(item: Any, *, index: int) -> str:
    if not isinstance(item, dict):
        raise ValueError(f"long_read_inputs[{index}] must be an object")
    path = item.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"long_read_inputs[{index}].path must be a non-empty string")
    return path


def _fail(state: TopLevelState, reason: str) -> StateUpdate:
    return update_t2t_stage_output(state, {"status": "failed", "reason": reason})
