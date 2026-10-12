"""Shared T2T threshold domain tests for finishing and execution consumers."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.llm.finishing import PARAMETERS
from app.schemas.t2t import T2TThresholds
from app.stages.t2t.parameters.execution import T2TFrozenExecutionInputs

_VALID_THRESHOLDS = {
    "min_motif_copies": 1,
    "terminal_window_bp": 1,
    "min_mapq": 255,
    "max_target_terminal_distance_bp": 0,
    "min_overlap_bp": 1,
    "min_supporting_reads": 1,
}


def _execution_payload(thresholds: dict[str, int]) -> dict[str, object]:
    return {
        "thresholds": thresholds,
        "resolved_telomere_motif": "TTAGGG",
        "motif_source": "provided_species_fact",
        "mode": "auto_patch",
        "input_assembly_ref": "assembly.fa",
        "work_dir": "/tmp/t2t",
        "execution_enabled": True,
    }


class T2TThresholdDomainTests(unittest.TestCase):
    def test_shared_consumers_accept_the_common_boundary_values(self):
        thresholds = T2TThresholds.model_validate(_VALID_THRESHOLDS)
        self.assertEqual(thresholds.min_mapq, 255)
        self.assertEqual(thresholds.max_target_terminal_distance_bp, 0)
        self.assertIsInstance(
            PARAMETERS["t2t"].model_validate(_VALID_THRESHOLDS), T2TThresholds
        )
        execution = T2TFrozenExecutionInputs.model_validate(
            _execution_payload(_VALID_THRESHOLDS)
        )
        self.assertEqual(execution.thresholds, thresholds)

    def test_shared_consumers_reject_each_out_of_domain_boundary(self):
        invalid_values = {
            "min_motif_copies": 0,
            "terminal_window_bp": 0,
            "min_mapq": -1,
            "max_target_terminal_distance_bp": -1,
            "min_overlap_bp": 0,
            "min_supporting_reads": 0,
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field, value=value):
                candidate = {**_VALID_THRESHOLDS, field: value}
                with self.assertRaises(ValidationError):
                    PARAMETERS["t2t"].model_validate(candidate)
                with self.assertRaises(ValidationError):
                    T2TFrozenExecutionInputs.model_validate(
                        _execution_payload(candidate)
                    )

        for value in (-1, 256):
            with self.subTest(field="min_mapq", value=value):
                candidate = {**_VALID_THRESHOLDS, "min_mapq": value}
                with self.assertRaises(ValidationError):
                    PARAMETERS["t2t"].model_validate(candidate)
                with self.assertRaises(ValidationError):
                    T2TFrozenExecutionInputs.model_validate(
                        _execution_payload(candidate)
                    )

    def test_shared_consumers_reject_boolean_thresholds(self):
        for field in _VALID_THRESHOLDS:
            with self.subTest(field=field):
                candidate = {**_VALID_THRESHOLDS, field: True}
                with self.assertRaises(ValidationError):
                    PARAMETERS["t2t"].model_validate(candidate)
                with self.assertRaises(ValidationError):
                    T2TFrozenExecutionInputs.model_validate(
                        _execution_payload(candidate)
                    )


if __name__ == "__main__":
    unittest.main()
