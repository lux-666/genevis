#!/usr/bin/env bash
# Scoped simplification gate; run in the activated development environment.
set -euo pipefail
cd "$(dirname "$0")/.."
npx --yes jscpd@5.0.16 app/knowledge --min-tokens 70 --format python --threshold 0 --reporters consoleFull --silent
python -m pytest -q tests/test_assembly_case_consumption.py tests/test_documentation_contracts.py
