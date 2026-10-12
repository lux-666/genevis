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
    (".bp.a_ctg.gfa", "alternate_contigs", "alternate_graph_bp"),
    (".hic.a_ctg.gfa", "alternate_contigs", "alternate_graph_hic"),
    (".hic.hap1.p_ctg.gfa", "haplotype_contigs", "hic_haplotype_1_graph"),
    (".hic.hap2.p_ctg.gfa", "haplotype_contigs", "hic_haplotype_2_graph"),
)


def _trio_output_declarations(
    prefix: Path,
    *,
    artifact_type: str,
) -> list[AssemblyNativeOutputDeclaration]:
    return [
        AssemblyNativeOutputDeclaration(
            path=prefix.parent / "pat.yak",
            artifact_type=artifact_type,
            artifact_class="other",
            native_role="trio.paternal_yak",
            media_type="application/octet-stream",
        ),
        AssemblyNativeOutputDeclaration(
            path=prefix.parent / "mat.yak",
            artifact_type=artifact_type,
            artifact_class="other",
            native_role="trio.maternal_yak",
            media_type="application/octet-stream",
        ),
        AssemblyNativeOutputDeclaration(
            path=Path(f"{prefix}.hap1.p_ctg.gfa"),
            artifact_type=artifact_type,
            artifact_class="haplotype_contigs",
            native_role="trio.hap1_graph",
            media_type="application/gfa",
        ),
        AssemblyNativeOutputDeclaration(
            path=Path(f"{prefix}.hap2.p_ctg.gfa"),
            artifact_type=artifact_type,
            artifact_class="haplotype_contigs",
            native_role="trio.hap2_graph",
            media_type="application/gfa",
        ),
    ]


def hifiasm_native_output_declarations(
    output_dir: Path,
    *,
    output_prefix: str,
    role_namespace: str,
    artifact_type: str,
    trio_enabled: bool = False,
    phased_fasta_dir: Path | None = None,
) -> list[AssemblyNativeOutputDeclaration]:
    """Declare the finite optional GFA set for one Hifiasm-family attempt."""

    prefix = output_dir / output_prefix
    declarations = [
        AssemblyNativeOutputDeclaration(
            path=Path(f"{prefix}{suffix}"),
            artifact_type=artifact_type,
            artifact_class=artifact_class,
            native_role=f"{role_namespace}.{role_suffix}",
            media_type="application/gfa",
        )
        for suffix, artifact_class, role_suffix in _GFA_OUTPUTS
    ]
    if trio_enabled:
        declarations.extend(
            _trio_output_declarations(
                prefix,
                artifact_type=artifact_type,
            )
        )
    if phased_fasta_dir is not None:
        phasing = "trio" if trio_enabled else "hic"
        declarations.extend(
            AssemblyNativeOutputDeclaration(
                path=phased_fasta_dir / f"hifiasm.hap{hap}.p_ctg.fasta",
                artifact_type="assembly.hifiasm_phased_fasta",
                artifact_class="haplotype_contigs",
                native_role=f"{phasing}.hap{hap}_fasta",
                media_type="application/fasta",
            )
            for hap in (1, 2)
        )
    return declarations
