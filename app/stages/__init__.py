"""Domain stage registry for the ONT/CLR genome workflow."""

STAGE_ORDER = [
    "inventory",
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
]

TERMINAL_STAGE = "done"
