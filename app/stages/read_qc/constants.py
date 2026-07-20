"""Method identifiers and planning labels for the read QC stage."""

METHOD_NAME = "read_qc.long_read_stats.seqkit_stats"
LENGTH_FILTER_METHOD_NAME = "read_qc.length_filter.seqkit_seq"
LENGTH_FILTER_SKIP = "skip"
LENGTH_FILTER_DECISIONS = {
    LENGTH_FILTER_SKIP: "skip filtering, use raw reads",
    "read_qc.length_filter.seqkit_seq": "seqkit_seq",
}
