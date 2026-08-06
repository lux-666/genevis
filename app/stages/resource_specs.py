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
LONG_READS_SPEC = ResourceSpec(
    resource_type="long_reads",
    asset_types=["clean_long_reads", "raw_long_reads"],
    asset_type_priority_groups=[
        ["clean_long_reads"],
        ["raw_long_reads"],
    ],
    allow_multiple=True,
)
SHORT_READS_SPEC = ResourceSpec(
    resource_type="short_reads",
    asset_types=["clean_short_dna_reads", "raw_short_dna_reads"],
    asset_type_priority_groups=[
        ["clean_short_dna_reads"],
        ["raw_short_dna_reads"],
    ],
    allow_multiple=True,
    required=False,
)
HIC_READS_SPEC = ResourceSpec(
    resource_type="hic_reads",
    asset_types=["hic_reads_registered"],
    allow_multiple=True,
)


def assembly_resource_spec() -> ResourceSpec:
    """Return the canonical current-assembly requirement."""

    return ASSEMBLY_SPEC
