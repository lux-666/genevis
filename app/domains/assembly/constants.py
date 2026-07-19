"""Assembly method identifiers and domain-level candidate sets."""

FLYE_METHOD = "assembly.candidate.flye"
NEXTDENOVO_METHOD = "assembly.candidate.nextdenovo"
HIFIASM_METHOD = "assembly.candidate.hifiasm"
HIFIASM_ONT_METHOD = "assembly.candidate.hifiasm_ont"

ASSEMBLY_AUTO_CANDIDATES = [
    FLYE_METHOD,
    NEXTDENOVO_METHOD,
]
ASSEMBLY_CANDIDATES = [
    *ASSEMBLY_AUTO_CANDIDATES,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
]
GENOME_SIZE_REQUIRED = {
    FLYE_METHOD,
    NEXTDENOVO_METHOD,
}
