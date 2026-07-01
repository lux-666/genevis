"""Shared constants for the assembly stage and its method graphs."""

ASSEMBLY_AUTO_CANDIDATES = [
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
]
ASSEMBLY_COMPATIBILITY_CANDIDATES = ["assembly.candidate.hifiasm"]
ASSEMBLY_CANDIDATES = [*ASSEMBLY_AUTO_CANDIDATES, *ASSEMBLY_COMPATIBILITY_CANDIDATES]
GENOME_SIZE_REQUIRED = {
    "assembly.candidate.nextdenovo",
}

FLYE_METHOD = "assembly.candidate.flye"
NEXTDENOVO_METHOD = "assembly.candidate.nextdenovo"
HIFIASM_METHOD = "assembly.candidate.hifiasm"

FLYE_READ_TYPE_FLAGS = {
    "nano-raw": "--nano-raw",
    "nano-hq": "--nano-hq",
    "pacbio-raw": "--pacbio-raw",
    "pacbio-hifi": "--pacbio-hifi",
}

# Hifiasm defaults to HiFi (no flag); ONT reads use --ont. CLR ("not_preferred")
# is intentionally excluded so it is rejected as an invalid read type.
HIFIASM_READ_TYPE_FLAGS = {
    "hifi": "",
    "ont": "--ont",
}

EXTRA_FAIL_STATUSES = frozenset({"blocked_param"})
