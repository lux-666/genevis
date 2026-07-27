"""Logical resource specifications and stage contracts.

Cross-stage handoff is expressed only in logical resource names. Stage-private
output keys remain in each stage's own checkpoint cursor and are not searched by
downstream stages.
"""

from app.stages.resource_resolution import ResourceSpec

ASSEMBLY_SPEC = ResourceSpec(
    resource_type="assembly",
    asset_types=["assembly_fasta_registered"],
)
ASSEMBLY_CANDIDATES_SPEC = ResourceSpec(
    resource_type="assembly",
    asset_types=["assembly_fasta_registered"],
    allow_multiple=True,
)
MASKED_ASSEMBLY_SPEC = ResourceSpec(
    resource_type="masked_assembly",
    required=False,
)
PREDICTED_PROTEINS_SPEC = ResourceSpec(
    resource_type="predicted_proteins",
)
LONG_READS_SPEC = ResourceSpec(
    resource_type="long_reads",
    asset_types=["clean_long_reads", "pacbio_hifi", "raw_long_reads", "long_reads"],
    asset_type_priority_groups=[
        ["clean_long_reads", "pacbio_hifi"],
        ["raw_long_reads", "long_reads"],
    ],
    allow_multiple=True,
)
SHORT_READS_SPEC = ResourceSpec(
    resource_type="short_reads",
    asset_types=["clean_short_dna_reads", "raw_short_dna_reads"],
    allow_multiple=True,
    required=False,
)
HIC_READS_SPEC = ResourceSpec(
    resource_type="hic_reads",
    asset_types=["hic_reads", "hi_c_reads", "hic_reads_registered"],
    allow_multiple=True,
    required=False,
)

# Compatibility names for callers that distinguish raw/clean manifest inputs.
RAW_READS_SPEC = LONG_READS_SPEC
CLEAN_READS_SPEC = ResourceSpec(
    resource_type="long_reads",
    asset_types=["clean_long_reads"],
    allow_multiple=True,
    required=False,
)


def assembly_resource_spec() -> ResourceSpec:
    """Return the canonical current-assembly requirement."""

    return ASSEMBLY_SPEC


STAGE_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "inventory": {
        "requires": [],
        "produces": ["long_reads", "short_reads", "hic_reads", "assembly"],
    },
    "read_qc": {"requires": ["long_reads"], "produces": ["long_reads"]},
    "genome_survey": {
        "requires": ["long_reads"],
        # Genome size is scalar evidence, not a path resource.
        "produces": [],
    },
    "assembly": {"requires": ["long_reads"], "produces": ["assembly"]},
    "assembly_dedup": {"requires": ["assembly"], "produces": ["assembly"]},
    "assembly_review": {"requires": ["assembly"], "produces": ["assembly"]},
    "hic_scaffolding": {"requires": ["assembly"], "produces": ["assembly"]},
    "t2t": {"requires": ["assembly"], "produces": ["assembly"]},
    "repeat_annotation": {
        "requires": ["assembly"],
        "produces": ["masked_assembly"],
    },
    "gene_annotation": {
        "requires": ["assembly", "masked_assembly"],
        "produces": ["predicted_proteins"],
    },
    "function_annotation": {
        "requires": ["predicted_proteins"],
        "produces": [],
    },
}


STAGE_REQUIREMENTS = {
    "read_qc": [{"name": "raw reads", "spec": LONG_READS_SPEC}],
    "genome_survey": [{"name": "reads", "spec": LONG_READS_SPEC}],
    "assembly": [{"name": "reads", "spec": LONG_READS_SPEC}],
    "assembly_dedup": [{"name": "draft assembly", "spec": ASSEMBLY_SPEC}],
    "assembly_review": [
        {"name": "assembly candidates", "spec": ASSEMBLY_CANDIDATES_SPEC}
    ],
    "hic_scaffolding": [{"name": "assembly", "spec": ASSEMBLY_SPEC}],
    "t2t": [{"name": "assembly", "spec": ASSEMBLY_SPEC}],
    "repeat_annotation": [{"name": "assembly", "spec": ASSEMBLY_SPEC}],
    "gene_annotation": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
        {"name": "masked assembly", "spec": MASKED_ASSEMBLY_SPEC},
    ],
    "function_annotation": [
        {"name": "predicted proteins", "spec": PREDICTED_PROTEINS_SPEC}
    ],
}
