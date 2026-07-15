"""Invariant: every registered method has a wired executable route."""
import unittest
from pathlib import Path
from app.schemas.descriptors import DescriptorRegistry
from app.stages.assembly.constants import ASSEMBLY_CANDIDATES
from app.stages.gene_annotation.nodes import GENE_EXECUTABLE_METHODS
from app.stages.function_annotation.nodes import FUNCTION_EXECUTABLE_METHODS
from app.stages.genome_survey.constants import GENOME_SURVEY_EXECUTABLE_METHODS

WIRED_RUNNERS = set().union(
    set(GENE_EXECUTABLE_METHODS),
    set(FUNCTION_EXECUTABLE_METHODS),
    set(ASSEMBLY_CANDIDATES),
    set(GENOME_SURVEY_EXECUTABLE_METHODS),
    {
        "assembly_dedup.purge_dups",
        "assembly_review.eval.quast",
        "assembly_review.eval.busco",
        "hic_scaffolding.candidate.juicer_3ddna_juicebox",
        "hic_scaffolding.candidate.yahs",
        "t2t.alignment.minimap2",
        "read_qc.long_read_stats.seqkit_stats",
        "read_qc.length_filter.seqkit_seq",
        "read_qc.make_fofn.local",
        "repeat_annotation.masking.repeatmasker",
    },
)


class RegistryExecutableInvariantTests(unittest.TestCase):
    def test_every_registered_method_has_a_wired_runner(self):
        descriptor_registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        registry = {
            method_name
            for method_name, descriptor in descriptor_registry.methods.items()
            if descriptor.planner.execution_kind == "local"
        }
        self.assertEqual(registry - WIRED_RUNNERS, set(), "registry methods with no wired runner")
        self.assertEqual(WIRED_RUNNERS - registry, set(), "wired runners with no registry descriptor")
