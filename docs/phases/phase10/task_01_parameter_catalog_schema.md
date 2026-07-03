# Task 1: Three-Level Parameter Catalog Schema

**Phase**: 10
**Status**: Pending
**Created**: 2026-07-02
**Updated**: 2026-07-03

## Goal

Define the catalog schema used by assembly methods to describe parameters with exactly three LLM permission levels:

- L0: LLM does not see and cannot modify.
- L1: LLM can see but cannot modify.
- L2: LLM can see and may modify.

Do not add secondary exposure switches or another expert bucket. If a parameter affects assembly behavior and is not a fixed fact, it belongs in L2.

## File Location

```text
descriptors/knowledge_packs/assembly/methods/{method_id}/params.yaml
```

Examples:

- `descriptors/knowledge_packs/assembly/methods/hifiasm/params.yaml`
- `descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml`

## Schema

```yaml
method_id: hifiasm_ont
tool: hifiasm
read_type: ont
description: "ONT simplex assembly mode"

parameters:
  output_prefix:
    cli: "-o"
    level: 0
    source: executor
    value_type: path
    description: "Output prefix"

  hg_size:
    cli: "--hg-size"
    level: 1
    source: genome_survey
    value_type: string
    source_priority:
      - genome_survey.genome_size_prior
      - genome_survey.estimates.genome_size
      - shared_parameters.estimated_genome_size
    description: "Estimated haploid genome size"

  purge_level:
    cli: "-l"
    level: 2
    source: parameter_recommender
    value_type: int
    allowed_values: [0, 1, 2, 3]
    default: 1
    description: "Purge duplication level"
```

## Field Definitions

| Field | Required | Meaning |
|---|---|---|
| `cli` | yes | CLI flag. Use `null` for positional input parameters. |
| `level` | yes | `0`, `1`, or `2`. This is the complete LLM permission model. |
| `source` | yes | Informational provenance: `executor`, `route_selector`, `read_qc`, `genome_survey`, `manifest`, `parameter_recommender`, etc. |
| `value_type` | yes | `int`, `float`, `string`, `boolean`, `path`, or `path_list`. |
| `allowed_values` | optional | Explicit enum for discrete values. |
| `min` / `max` | optional | Numeric bounds. |
| `default` | optional | Default if the parameter is not emitted. |
| `fixed_value` | optional | Fixed value for L0 route flags such as `--ont`. |
| `source_priority` | optional | Ordered state paths for L1 derivation. |
| `description` | optional | Human-readable meaning. |
| `notes` | optional | Additional caveats. |

## Level Definitions

| Level | Permission | Controller | Examples |
|---|---|---|---|
| L0 | LLM does not see and cannot modify | executor / route selector | `-o`, `-t`, input reads, attempt dirs, `--ont`, `--primary`, `-h`, `--version` |
| L1 | LLM can see but cannot modify | read_qc / genome_survey / manifest | `--hg-size`, `--hom-cov`, `--n-hap`, coverage, read N50, heterozygosity |
| L2 | LLM can see and may modify | parameter recommender / retry loop | `-l`, `-s`, `--purge-max`, `--rl-cut`, `--sc-cut`, `-k`, `-w`, `-D`, `-N`, graph thresholds |

## Python Models

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict


class ParameterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli: str | None
    level: Literal[0, 1, 2]
    source: str
    value_type: Literal["int", "float", "string", "boolean", "path", "path_list"]
    allowed_values: list[Any] | None = None
    min: int | float | None = None
    max: int | float | None = None
    default: Any = None
    fixed_value: Any = None
    source_priority: list[str] | None = None
    description: str | None = None
    notes: str | None = None


class MethodParameterCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method_id: str
    tool: str
    read_type: str
    description: str | None = None
    parameters: dict[str, ParameterSpec]
```

## Loader

```python
from pathlib import Path
import yaml

from app.schemas.parameter_catalog import MethodParameterCatalog


CATALOG_ROOT = Path("descriptors/knowledge_packs/assembly/methods")


def load_parameter_catalog(method_id: str) -> MethodParameterCatalog:
    path = CATALOG_ROOT / method_id / "params.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Parameter catalog not found: {path}")
    return MethodParameterCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
```

## Validation Rules

- L0 parameters must never enter the LLM prompt.
- L1 parameters may enter the prompt as read-only facts and must not appear in LLM output.
- L2 parameters are the only accepted keys in LLM output.
- L2 output must satisfy catalog `allowed_values`, `min`, `max`, and `rules.yaml`.
- Unknown LLM output keys are validation failures, not warnings.

## Acceptance Criteria

- [ ] Catalog schema implemented in `app/schemas/parameter_catalog.py`.
- [ ] Loader implemented in `app/stages/assembly/parameter_catalog.py`.
- [ ] Invalid levels are rejected.
- [ ] Unknown schema fields are rejected.
- [ ] Unit tests cover missing catalog, invalid level, and L1/L2 permission checks.
