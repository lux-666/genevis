"""Shared constants for the assembly stage and its method graphs."""

ASSEMBLY_AUTO_CANDIDATES = [
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
]
ASSEMBLY_COMPATIBILITY_CANDIDATES = ["assembly.candidate.hifiasm", "assembly.candidate.hifiasm_ont"]
ASSEMBLY_CANDIDATES = [*ASSEMBLY_AUTO_CANDIDATES, *ASSEMBLY_COMPATIBILITY_CANDIDATES]
GENOME_SIZE_REQUIRED = {
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
}

FLYE_METHOD = "assembly.candidate.flye"
NEXTDENOVO_METHOD = "assembly.candidate.nextdenovo"
HIFIASM_METHOD = "assembly.candidate.hifiasm"
HIFIASM_ONT_METHOD = "assembly.candidate.hifiasm_ont"

FLYE_READ_TYPE_FLAGS = {
    "nano-raw": "--nano-raw",
    "nano-hq": "--nano-hq",
    "pacbio-raw": "--pacbio-raw",
    "pacbio-hifi": "--pacbio-hifi",
}

# Hifiasm defaults to HiFi (no flag). ONT mode (--ont) requires FASTQ input.
# CLR ("not_preferred") is intentionally excluded so it is rejected as an invalid read type.
HIFIASM_READ_TYPE_FLAGS = {
    "hifi": "",
}

HIFIASM_ONT_READ_TYPE_FLAGS = {
    "ont": "--ont",
}

EXTRA_FAIL_STATUSES = frozenset({"blocked_param"})
