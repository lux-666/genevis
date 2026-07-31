# Parameter Recommendation

Assembly, AutoHiC, and T2T ask an LLM for parameters after the execution method
has already been selected. Every successfully parsed recommendation is shown
to a human before an execution attempt exists. All three stages use the strict
schema-3 input-snapshot and recommendation-manifest contract; pre-v3
recommendation roots are not accepted.

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

At each recommendation point, the stage first persists `input_snapshot.json`.
This schema-3 record contains typed canonical facts, a stage-specific context,
and immutable dependency refs. Quantitative facts carry their unit and semantic
origin. The strict model-facing DTO is built from the same validated facts and
context recorded in that snapshot, plus the immutable basis; storage paths,
session identity, and unrestricted source mappings never cross the model
boundary.

Assembly receives canonical biological observations, its typed assembly
context, the adjustable parameter descriptions, optional method
guidance/literature, and bounded ordered attempt history when applicable.
AutoHiC receives canonical species facts, library protocols, an explicitly
supplied enzyme if present, the complete adjustable L2 catalog, and bounded
ordered attempt history. T2T receives canonical species facts, the verified
motif resolution, the ONT input count, and its adjustable threshold catalog.

Species identity is used only when supplied explicitly through
`shared_parameters.species_facts`; it is not inferred from filenames,
free-form metadata, retrieval results, or another model. Before T2T threshold
recommendation or scanning, T2T persists a schema-3
`motif_input_snapshot.json`. A supplied
`shared_parameters.species_facts.telomere_motif` is validated and recorded as a
`provided_species_fact` resolution without an LLM call. Otherwise the motif
resolver uses the supplied scientific name and persists its exact
request/response/parse evidence. Missing scientific identity, an unresolved
response, or any pre-v3 motif/config checkpoint field fails closed. No common
repeat is assumed. The later T2T snapshot binds the verified motif-resolution
ref, and execution consumes only that resolved value and source.

Session IDs, run paths, artifact refs, hashes, credentials, and arbitrary asset
metadata are kept out of model input.

## Records

For recommendation `N`, inspect:

```text
artifacts/<stage>/parameter_recommendations/<N>/
├── input_snapshot.json
├── basis_snapshot.json        # initial basis; a retry may reuse its ref
├── model_request.json
├── model_response.json
├── recommendation.json
├── response_parse.json
└── recommendation_manifest.json
```

`recommendation.json` contains the exact parsed parameters.
`response_parse.json` reports only parsing success or failure. The manifest
binds all files by immutable refs and hashes; the approval decision is bound to
that manifest. On an Assembly retry, `basis_snapshot_ref` may deliberately
reuse the verified initial basis rather than publish a new copy.

`input_snapshot.json`, `basis_snapshot.json`, `model_request.json`, and the
recommendation manifest use schema 3. Response, parse, and recommendation
leaves retain their own declared schema versions. The strict v3 loader verifies
artifact types, hashes, stage/method/task identity, and the exact ref names;
pre-v3 recommendation refs and mixed-version roots fail closed.

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
    "read_inputs": [
      {"path": "clean_hifi.fastq.gz", "platform": "pacbio_hifi"},
      {"path": "ultralong_ont.fastq.gz", "platform": "ont"}
    ]
  }
}
```

The `species_facts.telomere_motif` value is an optional canonical supplied fact,
not a direct scan override. When present, it is validated and frozen through the
provided-fact motif-resolution chain. When absent, T2T resolves the motif from
the explicitly supplied scientific name through the model boundary. A T2T-local
motif config field or pre-v3 checkpoint value is rejected outright.

Each `t2t.read_inputs` entry requires both a path and an explicit platform:
`cycloneseq`, `ont`, `clr`, or `pacbio_hifi`. Relative paths are resolved
against the run-config file's directory while preserving list order. Legacy
ONT-only read fields are not part of the current contract.

T2T consumes the current logical `assembly` resource. An explicit
`t2t.assembly_fasta_ref`, when retained in a run config, is only an equality
assertion and must resolve to that same current assembly. It cannot select a
parallel FASTA or bypass the assembly adopted by Hi-C scaffolding.
