# Parameter Recommendation

Assembly and AutoHiC ask an LLM for parameters after the execution method has
already been selected. Every successfully parsed recommendation is shown to a
human before an execution attempt exists.

## What the model returns

The response has one exact shape:

```json
{
  "parameters": {"parameter_name": "value"},
  "summary": "Optional explanation for review."
}
```

Assembly retries also return `retry_disposition`. The system does not accept
`parameter_patch`, `params`, `recommended_params`, rich value wrappers, or
direct top-level parameter keys.

The parser checks JSON shape, not biological correctness. It does not reject an
unknown name or an unusual value merely because it is absent from a catalog or
outside a suggested range. The reviewer sees the exact parsed mapping and
chooses whether to approve it.

There is no automatic fallback to catalog defaults when the model is
unavailable or its response is malformed. That condition is recorded as
`parameter_response_invalid` and execution does not start.

## Model input

Assembly receives selected biological observations, the adjustable parameter
descriptions, optional method guidance/literature, and retry context when
applicable. AutoHiC receives only library protocol facts, an explicitly supplied
enzyme if present, and the `restriction_enzyme` parameter description.

Session IDs, run paths, artifact refs, hashes, credentials, and arbitrary asset
metadata are kept out of model input.

## Records

For recommendation `N`, inspect:

```text
artifacts/<stage>/parameter_recommendations/<N>/
├── input_profile.json
├── basis_snapshot.json
├── model_request.json
├── model_response.json
├── recommendation.json
├── response_parse.json
└── recommendation_manifest.json
```

`recommendation.json` contains the exact parsed parameters.
`response_parse.json` reports only parsing success or failure. The manifest
binds all files by immutable refs and hashes; the approval decision is bound to
that manifest.

After approval, the exact parameter mapping is handed to command construction.
If a name cannot be rendered safely for the selected tool, command freeze fails
explicitly; the system does not silently substitute a different value.
