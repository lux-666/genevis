# Parameter Recommendation

Assembly, AutoHiC, and T2T ask an LLM for parameters after the execution method
has already been selected. Every successfully parsed recommendation is shown
to a human before an execution attempt exists.

## What the model returns

The response has one exact shape:

```json
{
  "parameters": {"parameter_name": "value"},
  "summary": "Optional explanation for review."
}
```

`parameters` is sparse and may be `{}`. Initial and retry responses use this
same shape. The system does not accept `retry_disposition`, `parameter_patch`,
`params`, `recommended_params`, rich value wrappers, absent-key lists, or direct
top-level parameter keys.

The parser checks JSON shape, not biological correctness. It does not reject an
unknown name or an unusual value merely because it is absent from a catalog or
outside a suggested range. The reviewer sees the exact parsed mapping and
chooses whether to approve it.

This is the explicit L2 mapping for one attempt. It is not merged with catalog
defaults or a previous attempt, and it does not have to enumerate every L2
parameter. Omitted values remain supplied facts, executor-owned defaults, or
ordinary tool defaults. A retry supplies a fresh mapping; code does not derive
a delta or require any key to be retained, removed, or changed.

There is no automatic fallback to catalog defaults when the model is
unavailable or its response is malformed. That condition is recorded as
`parameter_response_invalid` and execution does not start.

## Model input

Assembly receives selected biological observations, the adjustable parameter
descriptions, optional method guidance/literature, and ordered attempt history
when applicable. AutoHiC receives library protocol facts, an explicitly
supplied enzyme if present, the complete adjustable L2 catalog, and bounded
ordered attempt history. T2T receives bounded assembly/read facts and its
adjustable threshold catalog.

Species identity is used only when supplied explicitly through
`shared_parameters.species_facts`; it is not inferred from filenames,
free-form metadata, retrieval results, or another model. Before T2T scans, a
small resolver prompt uses those facts (and any legacy motif hint) to produce
`resolved_telomere_motif`. The resolver stores its prompt/response evidence and
source. T2T scans and patch checks never read the legacy motif field directly.
If neither species facts nor a compatibility hint is supplied, T2T fails before
scanning rather than assuming `TTAGGG`.

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
If a name is unknown, belongs to another parameter level, or cannot be rendered
safely for the selected tool, command freeze fails explicitly; the system does
not silently substitute a different value.

For retries, the model also sees a bounded chronological projection of every
earlier attempt's recommendation, approval, frozen effective parameters,
execution result, and collect/review outcome. These records are context only.
Human decisions and operator retry policy decide whether another attempt is
created, and every shape-valid retry mapping returns to `parameter_approval`.

## Run-config inputs

Species facts, Hi-C retry policy, and explicit T2T read files are operator-owned
inputs. A current run-config can express them as follows:

```json
{
  "shared_parameters": {
    "species_facts": {
      "scientific_name": "Arabidopsis thaliana",
      "taxon_id": 3702,
      "ploidy": 2,
      "expected_chromosome_count": 5,
      "telomere_motif": "TTTAGGG"
    }
  },
  "hic_scaffolding": {
    "autohic_parameters": {
      "restriction_enzyme": "MboI",
      "threads": 24
    },
    "retry_on_execution_failure": true,
    "max_execution_retries": 1,
    "max_review_retries": 1
  },
  "t2t": {
    "mode": "auto_patch",
    "assembly_fasta_ref": "assembly.fasta",
    "ont_read_refs": ["clean_lane1.fastq.gz", "clean_lane2.fastq.gz"]
  }
}
```

The `species_facts.telomere_motif` value is optional compatibility evidence,
not a direct scan override. When species facts are present, T2T asks the motif
resolver to reconcile the identity and hint; the resulting resolved value is
the only motif consumed by execution.

Relative paths in `ont_read_refs` are resolved independently against the
run-config file's directory while preserving list order. The singular
`ont_reads_ref` field is not part of the current T2T contract.
