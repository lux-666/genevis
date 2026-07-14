"""Resource specifications for common stage inputs.

Defines ResourceSpec instances for standard resources used across stages.
These specs can be used with resolve_stage_resource() for consistent resolution.

TODO: Future enhancement - generate/refine via LLM + knowledge base.
Current: Hardcoded based on current stage implementations.
"""

from app.stages.base import ResourceSpec

# ============================================================================
# Assembly Resources
# ============================================================================

ASSEMBLY_SPEC = ResourceSpec(
    resource_type="assembly",
    prefer_stages=["t2t", "hic_scaffolding", "assembly_review"],
    output_keys={
        "t2t": ["final_assembly_for_annotation"],
        "hic_scaffolding": ["final_assembly_for_annotation", "chromosome_scaffolded_assembly_candidate_ref"],
        "assembly_review": ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"],
    },
    asset_types=["assembly_fasta_registered"],
    required=True,
)

MASKED_ASSEMBLY_SPEC = ResourceSpec(
    resource_type="masked_assembly",
    prefer_stages=["repeat_annotation"],
    output_keys={
        "repeat_annotation": ["softmasked_genome_ref"],
    },
    asset_types=[],  # Should not come from asset registry
    required=False,
)

PREDICTED_PROTEINS_SPEC = ResourceSpec(
    resource_type="predicted_proteins",
    prefer_stages=["gene_annotation"],
    output_keys={
        "gene_annotation": ["proteins_fasta_ref", "predicted_proteins_ref", "predicted_proteins"],
    },
    asset_types=[],  # Function annotation only consumes gene_annotation outputs
    required=True,
)

# ============================================================================
# Stage Output Registry
# ============================================================================

STAGE_OUTPUTS = {
    "read_qc": ["assembly_input_ref", "filtered_long_reads_ref"],
    "assembly": ["draft_assembly_ref", "selected_candidate_refs"],
    "assembly_dedup": [
        "purged_assembly_ref",
        "purged_candidate_ref",
        "final_assembly_for_scaffolding_or_annotation",
    ],
    "assembly_review": ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"],
    "hic_scaffolding": ["final_assembly_for_annotation", "chromosome_scaffolded_assembly_candidate_ref"],
    "t2t": ["final_assembly_for_annotation"],
    "repeat_annotation": ["softmasked_genome_ref"],
    "gene_annotation": ["proteins_fasta_ref", "predicted_proteins_ref", "predicted_proteins"],
}

# ============================================================================
# Reads Resources
# ============================================================================

RAW_READS_SPEC = ResourceSpec(
    resource_type="raw_reads",
    prefer_stages=[],  # No stage produces raw reads
    output_keys={},
    asset_types=["raw_long_reads", "long_reads"],
    required=True,
)

CLEAN_READS_SPEC = ResourceSpec(
    resource_type="clean_reads",
    prefer_stages=["read_qc"],
    output_keys={
        "read_qc": ["filtered_long_reads_ref", "assembly_input_ref"],
    },
    asset_types=["clean_long_reads"],
    required=False,  # Can fallback to raw
)

# ============================================================================
# Stage Requirements Registry
# ============================================================================

STAGE_REQUIREMENTS = {
    "read_qc": [
        {
            "name": "raw reads",
            "spec": RAW_READS_SPEC,
        },
    ],
    "genome_survey": [
        {
            "name": "reads",
            "spec": ResourceSpec(
                resource_type="reads",
                prefer_stages=["read_qc"],
                output_keys={
                    "read_qc": [
                        "assembly_input_ref",
                        "filtered_long_reads_fofn_ref",
                        "filtered_long_reads_ref",
                        "filtered_long_reads_refs",
                        "long_reads_fofn_ref",
                    ]
                },
                asset_types=["clean_long_reads", "raw_long_reads", "clean_short_dna_reads", "raw_short_dna_reads"],
                required=True,
            ),
        },
    ],
    "assembly": [
        {
            "name": "reads",
            "spec": ResourceSpec(
                resource_type="reads",
                prefer_stages=["read_qc"],
                output_keys={"read_qc": ["assembly_input_ref", "filtered_long_reads_ref"]},
                asset_types=["clean_long_reads", "raw_long_reads"],
                required=True,
            ),
        },
    ],
    "assembly_dedup": [
        {
            "name": "draft assembly",
            "spec": ResourceSpec(
                resource_type="draft_assembly",
                prefer_stages=["assembly"],
                output_keys={"assembly": ["draft_assembly_ref"]},
                asset_types=[],
                required=True,
            ),
        },
    ],
    "assembly_review": [
        {
            "name": "assembly candidates",
            "spec": ResourceSpec(
                resource_type="assembly_candidates",
                prefer_stages=["assembly"],
                output_keys={"assembly": ["selected_candidate_refs"]},
                asset_types=["assembly_fasta_registered"],  # For existing_assembly route
                required=True,
            ),
        },
    ],
    "hic_scaffolding": [
        {
            "name": "assembly",
            "spec": ASSEMBLY_SPEC,
        },
    ],
    "t2t": [
        {
            "name": "assembly",
            "spec": ASSEMBLY_SPEC,
        },
    ],
    "repeat_annotation": [
        {
            "name": "assembly",
            "spec": ASSEMBLY_SPEC,
        },
    ],
    "gene_annotation": [
        {
            "name": "assembly",
            "spec": ASSEMBLY_SPEC,
        },
        {
            "name": "masked assembly",
            "spec": MASKED_ASSEMBLY_SPEC,
        },
    ],
    "function_annotation": [
        {
            "name": "predicted proteins",
            "spec": PREDICTED_PROTEINS_SPEC,
        },
    ],
}
