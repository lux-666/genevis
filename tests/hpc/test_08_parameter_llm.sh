#!/usr/bin/env bash
# Exercise only the production Assembly parameter LLM boundary. No bioinformatics
# tools are started; the test replays an existing model request artifact.
set -euo pipefail
cd "$(dirname "$0")/../../.."

MODEL_REQUEST="${1:-${PARAMETER_LLM_SMOKE_REQUEST:-}}"
if [[ -z "$MODEL_REQUEST" && -n "${TEST_DATA_ROOT:-}" && -d "${TEST_DATA_ROOT}/runs" ]]; then
  MODEL_REQUEST=$(
    find "${TEST_DATA_ROOT}/runs" -type f \
      -path '*/assembly/parameter_recommendations/*/model_request.json' \
      | sort | tail -n 1 || true
  )
fi
if [[ -z "$MODEL_REQUEST" ]]; then
  echo "skip: no parameter model request found; pass one explicitly" >&2
  exit 77
fi
if [[ ! -s "$MODEL_REQUEST" ]]; then
  echo "fail: model request is missing or empty: $MODEL_REQUEST" >&2
  exit 1
fi

OUTPUT_DIR="${PARAMETER_LLM_SMOKE_OUTPUT:-${TEST_DATA_ROOT:-$(mktemp -d)}/parameter_llm_smoke}"
mkdir -p "$OUTPUT_DIR"

python3 -m app.diagnostics.parameter_recommendation_smoke \
  --model-request "$MODEL_REQUEST" \
  --output-dir "$OUTPUT_DIR"
