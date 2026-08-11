from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest
import yaml
from pydantic import ValidationError

from app.configuration import load_descriptor_registry
from app.schemas.descriptors import (
    DescriptorRegistry,
    IOSpec,
    MethodDescriptor,
    PackageDescriptor,
    PlannerConditionalRequirement,
    PlannerMetadata,
    RegistryDocument,
    ResourceHints,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_descriptor_registry_loading_is_cwd_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    registry = load_descriptor_registry()

    assert "assembly.candidate.flye" in registry.methods
    assert len(registry.methods) == 14
    assert registry.methods["assembly_dedup.purge_dups"].defaults == {
        "cutoffs_strategy": "calcuts_auto",
        "get_seqs_exclude_haplotigs": True,
    }
    busco = registry.packages["busco"]
    assert busco.version_constraint == ">=5.4"
    assert busco.conditional_version_constraints == {
        "lineage_version=odb12": ">=5.8",
        "lineage_version=odb12.2": ">=6.1",
    }
    assert "--offline" in busco.entry_patterns[0]
    assert "--auto-lineage" not in busco.entry_patterns[0]
    assert registry.root_dir.name == "descriptors"


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ResourceHints, {"undeclared": True}),
        (
            IOSpec,
            {
                "name": "reads",
                "type": "fastq",
                "description": "Reads.",
                "undeclared": True,
            },
        ),
        (PlannerConditionalRequirement, {"undeclared": True}),
        (PlannerMetadata, {"undeclared": True}),
        (
            PackageDescriptor,
            {
                "package_name": "tool",
                "version_constraint": ">=1",
                "language": "cli",
                "install_channel": "conda",
                "entry_patterns": ["tool"],
                "help_summary": "A tool.",
                "undeclared": True,
            },
        ),
        (
            MethodDescriptor,
            {
                "method_name": "stage.method",
                "stage": "stage",
                "package_ref": "tool",
                "goal": "Run a tool.",
                "required_inputs": [],
                "expected_outputs": [],
                "undeclared": True,
            },
        ),
        (
            RegistryDocument,
            {"packages": {}, "methods": {}, "by_stage": {}, "undeclared": True},
        ),
    ],
)
def test_descriptor_yaml_models_reject_undeclared_fields(model, payload) -> None:
    with pytest.raises(ValidationError, match="undeclared"):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("relative_path", "add_undeclared"),
    [
        ("registry.yaml", lambda document: document.__setitem__("undeclared", {})),
        (
            "methods/assembly/flye_candidate.yaml",
            lambda document: document.__setitem__("undeclared", True),
        ),
        (
            "methods/assembly/flye_candidate.yaml",
            lambda document: document.__setitem__("planner", {"undeclared": True}),
        ),
        (
            "methods/assembly/flye_candidate.yaml",
            lambda document: document["required_inputs"][0].__setitem__(
                "undeclared", True
            ),
        ),
        (
            "methods/assembly/flye_candidate.yaml",
            lambda document: document["resource_hints"].__setitem__("undeclared", True),
        ),
        (
            "packages/flye.yaml",
            lambda document: document.__setitem__("undeclared", True),
        ),
        (
            "packages/flye.yaml",
            lambda document: document["resource_defaults"].__setitem__(
                "undeclared", True
            ),
        ),
    ],
)
def test_descriptor_registry_rejects_undeclared_yaml_fields(
    tmp_path, relative_path, add_undeclared
) -> None:
    descriptor_root = tmp_path / "descriptors"
    copytree(PROJECT_ROOT / "descriptors", descriptor_root)
    path = descriptor_root / relative_path
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    add_undeclared(document)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)

    with pytest.raises(ValidationError, match="undeclared"):
        DescriptorRegistry.load(descriptor_root / "registry.yaml")
