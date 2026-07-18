"""Failure-state contracts for non-annotation stage routers."""

from __future__ import annotations

import unittest

from app.stages.assembly_review.routing import (
    route_after_busco,
    route_after_comparison,
    route_after_parameter_quality_gate,
    route_after_plan_metrics,
    route_after_quast,
    route_from_evaluation,
)
from app.stages.genome_survey.routing import (
    route_after_full_plan,
    route_after_genomescope2,
    route_after_jellyfish,
    route_after_plan,
)
from app.stages.genome_survey.routing import (
    route_after_resolve as route_after_survey_resolve,
)
from app.stages.genome_survey.routing import (
    route_after_select as route_after_survey_select,
)
from app.stages.hic_scaffolding.routing import (
    route_after_autohic,
    route_after_recommendation,
    route_after_review,
)
from app.stages.hic_scaffolding.routing import (
    route_after_resolve as route_after_hic_resolve,
)
from app.stages.inventory.routing import route_from_inventory
from app.stages.read_qc.routing import (
    route_after_length_filter,
    route_after_make_fofn,
    route_after_stats,
)
from app.stages.read_qc.routing import (
    route_after_plan as route_after_qc_plan,
)
from app.stages.read_qc.routing import (
    route_after_resolve as route_after_qc_resolve,
)
from app.stages.t2t.routing import (
    route_after_alignment_freeze,
    route_after_alignment_run,
    route_after_assembly_scan,
    route_after_candidates,
    route_after_patches,
    route_after_read_prepare,
    route_after_read_scan,
    route_after_report,
    route_after_validation,
)
from app.stages.t2t.routing import (
    route_after_resolve as route_after_t2t_resolve,
)

BASE_FAILURES = ("failed", "blocked_env", "missing_tool")


def stage_state(stage: str, status: str, **fields: object) -> dict:
    return {"stage_outputs": {stage: {"status": status, **fields}}}


class RoutingFailureContractTests(unittest.TestCase):
    def test_assembly_review_routes_shared_failures_to_fail(self):
        routers = (
            route_after_plan_metrics,
            route_after_quast,
            route_after_busco,
            route_after_parameter_quality_gate,
            route_from_evaluation,
            route_after_comparison,
        )
        for status in BASE_FAILURES:
            for router in routers:
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(
                        router(stage_state("assembly_review", status)), "fail"
                    )

    def test_inventory_routes_shared_failures_to_fail(self):
        for status in BASE_FAILURES:
            with self.subTest(status=status):
                self.assertEqual(
                    route_from_inventory(stage_state("inventory", status)), "fail"
                )

    def test_genome_survey_routes_shared_failures_to_fail(self):
        routers = (
            route_after_survey_resolve,
            route_after_survey_select,
            route_after_full_plan,
            route_after_jellyfish,
            route_after_genomescope2,
            route_after_plan,
        )
        for status in BASE_FAILURES:
            for router in routers:
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(
                        router(stage_state("genome_survey", status)), "fail"
                    )

    def test_t2t_routes_shared_failures_to_fail(self):
        routers = (
            route_after_t2t_resolve,
            route_after_assembly_scan,
            route_after_read_prepare,
            route_after_read_scan,
            route_after_alignment_freeze,
            route_after_alignment_run,
            route_after_candidates,
            route_after_patches,
            route_after_validation,
            route_after_report,
        )
        for status in BASE_FAILURES:
            for router in routers:
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(router(stage_state("t2t", status)), "fail")

    def test_hic_routes_execution_failures_to_fail_and_requirements_to_gate(self):
        for status in BASE_FAILURES:
            for router in (route_after_recommendation, route_after_review):
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(
                        router(stage_state("hic_scaffolding", status)), "fail"
                    )

        for status in ("blocked_env", "missing_tool"):
            for router in (route_after_hic_resolve, route_after_autohic):
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(
                        router(stage_state("hic_scaffolding", status)), "requirements"
                    )

    def test_read_qc_routes_shared_and_stage_specific_failures_to_fail(self):
        for status in (*BASE_FAILURES, "blocked_input"):
            for router in (
                route_after_qc_resolve,
                route_after_qc_plan,
                route_after_stats,
                route_after_length_filter,
                route_after_make_fofn,
            ):
                with self.subTest(status=status, router=router.__name__):
                    self.assertEqual(router(stage_state("read_qc", status)), "fail")


if __name__ == "__main__":
    unittest.main()
