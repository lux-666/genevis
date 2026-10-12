"""Hi-C paired-read fastp adapter."""

from app.execution.lifecycle import (
    CompiledMethodGraph,
    build_method_graph,
)
from app.stages.read_qc.constants import HIC_READS_FASTP_METHOD_NAME
from app.stages.read_qc.method_application.fastp import (
    collect_fastp_artifacts,
    fastp_lifecycle,
    freeze_fastp_attempt,
)

METHOD_NAME = HIC_READS_FASTP_METHOD_NAME
LIFECYCLE = fastp_lifecycle("hic_reads")


def build_hic_reads_fastp_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE,
        lambda state: freeze_fastp_attempt(state, "hic_reads", LIFECYCLE),
        lambda state: collect_fastp_artifacts(state, "hic_reads", LIFECYCLE),
    )
