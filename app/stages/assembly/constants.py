"""Shared constants for the assembly stage and its method graphs."""

ASSEMBLY_AUTO_CANDIDATES = [
    "assembly.candidate.flye",
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
]
ASSEMBLY_COMPATIBILITY_CANDIDATES = ["assembly.candidate.hifiasm"]
ASSEMBLY_CANDIDATES = [*ASSEMBLY_AUTO_CANDIDATES, *ASSEMBLY_COMPATIBILITY_CANDIDATES]
GENOME_SIZE_REQUIRED = {
    "assembly.candidate.nextdenovo",
    "assembly.candidate.canu",
}

FLYE_METHOD = "assembly.candidate.flye"
NEXTDENOVO_METHOD = "assembly.candidate.nextdenovo"

FLYE_READ_TYPE_FLAGS = {
    "nano-raw": "--nano-raw",
    "nano-hq": "--nano-hq",
    "pacbio-raw": "--pacbio-raw",
    "pacbio-hifi": "--pacbio-hifi",
}
FLYE_REQUIRED_PARAMS = ("read_type", "estimated_genome_size", "threads")

FAIL_STATUSES = {"failed", "blocked_env", "missing_tool", "blocked_param"}
