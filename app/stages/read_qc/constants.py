"""Method identifiers and planning labels for the read QC stage."""

METHOD_NAME = "read_qc.long_read_stats.seqkit_stats"
LENGTH_FILTER_METHOD_NAME = "read_qc.length_filter.seqkit_seq"
LENGTH_FILTER_SKIP = "skip"
LENGTH_FILTER_SKIP_METHOD_NAME = "read_qc.length_filter.skip"
SHORT_READS_FASTP_METHOD_NAME = "read_qc.short_reads.fastp"
HIC_READS_FASTP_METHOD_NAME = "read_qc.hic_reads.fastp"
LENGTH_FILTER_DECISIONS = {
    LENGTH_FILTER_SKIP: "skip filtering, use raw reads",
    "read_qc.length_filter.seqkit_seq": "seqkit_seq",
}

FASTP_DEFAULTS = {
    "threads": 4,
    "qualified_quality_phred": 15,
    "unqualified_percent_limit": 40,
    "n_base_limit": 5,
    "length_required": 50,
}
