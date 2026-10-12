"""Both advisors see the same explicitly bound assembly material roles."""

from app.domains.assembly.constants import HIFIASM_METHOD
from app.domains.assembly.material_query import build_material_query, visible_query
from app.domains.assembly.selection import AssemblySelection
from app.stages.assembly.parameters.context import build_assembly_recommendation_context


def test_method_and_parameter_advisors_share_bound_roles():
    output = {
        "assembly_selection": AssemblySelection(
            read_type="pacbio_hifi",
            primary_method=HIFIASM_METHOD,
            source="human",
            rationale="joint material",
        ).model_dump(mode="json"),
        "selected_route": "hifi",
        "target_level": "near_t2t",
        "assembly_input_member_platforms": {"primary": ["pacbio_hifi", "pacbio_hifi"]},
        "use_ultralong_ont": True,
        "ultralong_read_refs": ["ul-1", "ul-2"],
        "trio_enabled": True,
        "trio_maternal_read_refs": ["maternal"],
        "trio_paternal_read_refs": ["paternal-1", "paternal-2"],
    }
    method_context = visible_query(build_material_query({}, output))["material_inputs"]
    parameter_context = build_assembly_recommendation_context({}, assembly_out=output)
    assert method_context == parameter_context.material_inputs.model_dump(mode="json")
    assert method_context == {
        "hifi_files": 2,
        "ont_files": 0,
        "ultralong_ont_files": 2,
        "hic_pairs": 0,
        "trio_maternal_files": 1,
        "trio_paternal_files": 2,
        "phasing_mode": "trio",
    }


def test_ordinary_ont_and_hic_presence_do_not_invent_bound_roles():
    query = build_material_query(
        {},
        {
            "assembly_input_member_platforms": {"primary": ["pacbio_hifi", "ont"]},
            "has_hic": True,
            "ont_reads_ref": "ordinary-ont",
            "hic_reads_ref": "scaffolding-only",
        },
    )
    context = visible_query(query)["material_inputs"]
    assert context["hifi_files"] == context["ont_files"] == 1
    assert context["ultralong_ont_files"] == context["hic_pairs"] == 0
    assert context["phasing_mode"] == "none"
