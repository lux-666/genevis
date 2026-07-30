"""Method-owned declarations for Hifiasm-family native outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeAlias

from app.stages.assembly.method_application.support import (
    AssemblyNativeOutputDeclaration,
)

NativeArtifactClass: TypeAlias = Literal[
    "assembly_graph",
    "alternate_contigs",
    "haplotype_contigs",
    "report",
    "config",
    "log",
    "other",
]

_GFA_OUTPUTS: tuple[tuple[str, NativeArtifactClass, str], ...] = (
    (".bp.p_ctg.gfa", "assembly_graph", "primary_graph_bp"),
    (".hic.p_ctg.gfa", "assembly_graph", "primary_graph_hic"),
    (".p_ctg.gfa", "assembly_graph", "primary_graph_legacy"),
    (".bp.a_ctg.gfa", "alternate_contigs", "alternate_graph_bp"),
    (".a_ctg.gfa", "alternate_contigs", "alternate_graph_legacy"),
    (".hic.hap1.p_ctg.gfa", "haplotype_contigs", "hic_haplotype_1_graph"),
    (".hic.hap2.p_ctg.gfa", "haplotype_contigs", "hic_haplotype_2_graph"),
    (".hap1.p_ctg.gfa", "haplotype_contigs", "haplotype_1_graph"),
    (".hap2.p_ctg.gfa", "haplotype_contigs", "haplotype_2_graph"),
)


def hifiasm_native_output_declarations(
    output_dir: Path,
    *,
    output_prefix: str,
    role_namespace: str,
    artifact_type: str,
) -> list[AssemblyNativeOutputDeclaration]:
    """Declare the finite optional GFA set for one Hifiasm-family attempt."""

    prefix = output_dir / output_prefix
    return [
        AssemblyNativeOutputDeclaration(
            path=Path(f"{prefix}{suffix}"),
            artifact_type=artifact_type,
            artifact_class=artifact_class,
            native_role=f"{role_namespace}.{role_suffix}",
            media_type="application/gfa",
        )
        for suffix, artifact_class, role_suffix in _GFA_OUTPUTS
    ]
