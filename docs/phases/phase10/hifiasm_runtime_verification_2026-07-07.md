# hifiasm Runtime Verification - 2026-07-07

This note records the binary-backed catalog verification used to close Phase 10
tasks 02 and 03.

## Environment

- Binary: `/opt/homebrew/Caskroom/miniconda/base/envs/env_geno_exec/bin/hifiasm`
- Reported version: `0.25.0-r726`
- Project constraint: `descriptors/packages/hifiasm.yaml` declares
  `version_constraint: ">=0.21.0"`
- Verification date: 2026-07-07

## Commands

```bash
HIFIASM_BIN=/opt/homebrew/Caskroom/miniconda/base/envs/env_geno_exec/bin/hifiasm \
  conda run -n gene_agent python -m unittest tests.test_phase10_parameter_catalog -v
```

For release verification where skipping is not acceptable:

```bash
PHASE10_REQUIRE_HIFIASM_RUNTIME=1 \
HIFIASM_BIN=/opt/homebrew/Caskroom/miniconda/base/envs/env_geno_exec/bin/hifiasm \
  conda run -n gene_agent python -m unittest tests.test_phase10_parameter_catalog.ParameterCatalogSchemaTests.test_runtime_hifiasm_help_flags_are_cataloged_when_binary_available -v
```

## Result

The runtime help check parsed 66 CLI flags from `hifiasm -h`. The advertised
surface included `--ont`, `--chem-c`, `--chem-f`, `--rl-cut`, and `--sc-cut`.
Every advertised help flag was present in both the `hifiasm` and `hifiasm_ont`
parameter catalogs.

Flags documented in the catalog but not advertised by `hifiasm -h` remain
tracked as the upstream help/CommandLines.cpp-derived surface; they are not
proven by the runtime `-h` output alone.
