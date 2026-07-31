# ArtifactRef Method Handoff Design

## Problem

Genome Survey publishes the Jellyfish database and histogram through the v2
artifact index, but its stage cursor stores their absolute paths in fields named
`kmer_db_artifact_ref` and `kmer_hist_artifact_ref`. GenomeScope2 now freezes
the histogram as an `ArtifactInputDependency`, whose contract correctly requires
a complete `ArtifactRef`. The path masquerading as a ref therefore fails Pydantic
validation before GenomeScope2 executes.

The regression was exposed when strict frozen input dependencies were added
without migrating the older Genome Survey producer and its tests.

## Contract

Run-owned method outputs have two distinct representations:

- execution path: an absolute path used in generated commands;
- artifact identity: an `ArtifactRefJson` containing the indexed relative path,
  digest, size, type, and stable artifact ID.

Fields ending in `_path` or path-oriented legacy `_ref` fields may remain
strings. Fields ending in `_artifact_ref` must contain an `ArtifactRefJson` when
they are consumed as artifact dependencies.

`ArtifactInputDependency` remains strict. It must not accept bare strings,
because doing so would bypass the index-backed identity and integrity check.

## Implementation

After Jellyfish publishes its outputs, recover their canonical indexed refs with
`artifact_ref_for_path` and store those refs in `kmer_db_artifact_ref` and
`kmer_hist_artifact_ref`. Keep `kmer_db_path` and `kmer_hist_path` as absolute
paths for command construction. Convert each returned `ArtifactRef` model with
`model_dump(mode="json")` before writing the typed checkpoint cursor.

During the compatibility window, type the two Genome Survey artifact fields as
`ArtifactRefJson | str` so old `jellyfish_collected` checkpoints can still pass
cursor validation and route to GenomeScope2. Normalize both legacy database and
histogram strings at the GenomeScope2 freeze boundary. The normalizer resolves
each path through `artifact_ref_for_path` with its exact expected artifact type,
constructs the strict dependency, and persists the canonical `ArtifactRefJson`
values in the frozen checkpoint update. New producer output therefore always
contains mappings, while one release can resume old checkpoints without
weakening downstream dependency validation.

Missing, unindexed, wrong-type, or byte-drifted legacy paths fail closed during
freeze. New mapping refs are also checked for exact type, indexed identity,
command-path equality, and current bytes. GenomeScope2 includes the verified
histogram dependency in its frozen attempt.

Read QC outputs require the same provenance distinction. When the selected long
reads come from a `ReadSetManifest`, Genome Survey resolves each member through
the shared runtime read resolver and carries its `output_ref` as
`read_artifact_ref`. Jellyfish freezes those derived reads as artifact input
dependencies. Registry-owned raw reads continue to use asset input dependencies.
This avoids treating a Read QC output path as though it were the path of its
source registry asset.

## Method Audit

Audit every `artifact_input_dependency` call site. For each call, trace the ref
argument to its producer and verify that it is one of:

- an `ArtifactRef` instance;
- an `ArtifactRefJson` mapping;
- a path that is explicitly converted with `artifact_ref_for_path` before the
  dependency is constructed.

Do not convert path-only report fields or external asset paths merely because
their names contain `ref`. Scope code changes to executable method handoffs
that cross the strict artifact dependency boundary.

## Tests

Add checksum-ready Genome Survey fixtures so Jellyfish reaches the GenomeScope2
handoff. Assert that:

- Jellyfish checkpoint fields contain complete `ArtifactRefJson` values;
- the refs resolve to the same paths used by the commands;
- the frozen GenomeScope2 attempt contains an artifact dependency with the
  expected type and identity;
- an actual legacy `jellyfish_collected` checkpoint containing string database
  and histogram refs routes successfully, then upgrades both refs during
  GenomeScope2 freeze;
- an unindexed legacy path is rejected;
- an indexed legacy artifact whose file is missing is rejected;
- a legacy path indexed with the wrong artifact type is rejected;
- a legacy path whose bytes drifted after indexing is rejected;
- Read QC filtered long reads are frozen from their manifest-owned artifact refs
  rather than being misidentified as raw registry assets;
- the full local Genome Survey path completes.

Run the focused Genome Survey tests, input-integrity tests, all method-stage
tests touched by the audit, and the repository's standard unit test suite if
runtime permits.
