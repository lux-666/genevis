# Task 01: Correct the T2T Patch and ONT Input Contract

**Phase**: 13
**Status**: Completed
**Depends on**: None

## Goal

Make T2T input resolution and patch materialization correct before parameter
recommendation is added. This task removes two correctness problems from the
current stage:

1. only the first registered ONT asset is consumed;
2. a candidate whose alignment stops before the target terminus appends the
   read tail to the complete contig and therefore retains the target suffix
   that should have been replaced at the junction.

The task also moves the no-read branch onto the normal work-to-immutable
artifact publication path.

## Current State

- `_ont_reads_ref()` returns the first matching clean or raw long-read asset.
- `ont_reads_ref` and `ont_reads_path` are singular stage fields.
- read scans do not retain the source asset for each read, so duplicate read
  identifiers across files are ambiguous.
- candidate records keep `target_distance_bp` but do not retain the exact
  target junction coordinate required by patch materialization.
- `_write_patched_fasta()` always constructs `left_tail + full_contig +
  right_tail`.
- `_skip_no_reads()` writes directly below the artifacts directory instead of
  publishing files written in the stage work directory.

## Design

### Canonical ONT input list

Use `ont_read_refs: list[str]` as the canonical stage field. Input resolution
must collect every usable ONT asset in stable registry order:

1. prefer all `clean_long_reads` assets when at least one is present;
2. otherwise use all `raw_long_reads` assets;
3. reject missing, non-file, empty, or non-FASTA/FASTQ members as a bounded
   input failure instead of silently dropping them;
4. do not select only the first asset.

The run-config, planner, materialized run config, stage state, and finishing
bundle must all use the list form. `app/storage/materialization.py` must resolve
every relative member against the source run-config directory and persist only
absolute list members. The canonical materialized contract must not retain the
singular `ont_reads_ref` field.

The read scan iterates the resolved files in order. Scan records and retained
telomeric-read records must identify the source asset. Read identifiers used by
later joins must be unique across the complete list. Either preserve a unique
registered asset ID with the original read ID or fail clearly on a duplicate;
never let the last duplicate silently overwrite the first in a dictionary.

An empty resolved list is not a malformed-input failure. It takes the no-read
path before parameter recommendation or sequence scanning. A non-empty list is
validated member by member, and any malformed member fails the stage instead of
being converted into the no-read outcome.

### Junction-aware patch model

Each selected candidate must retain enough immutable evidence to reconstruct
the junction:

- target name and target end;
- target start and target end coordinates from the PAF record;
- normalized junction coordinate;
- source read, read end, strand, query start, and query end;
- replacement tail sequence identity or the data needed to reproduce it;
- `target_distance_bp` and the number of target bases trimmed;
- map quality, overlap, and supporting read IDs.

For a right-end extension, keep the target sequence through the aligned target
end and replace the remaining suffix with the oriented read tail:

```text
patched = target[:target_end] + oriented_read_tail
```

For a left-end extension, replace the target prefix before the aligned target
start:

```text
patched = oriented_read_tail + target[target_start:]
```

Tail orientation is defined from the PAF query coordinates as follows. First
orient the complete read into target-forward coordinates: retain the read for
`strand=+`, and reverse-complement the complete read for `strand=-`. The aligned
query interval in that oriented read is `[query_start, query_end)` for `+` and
`[query_length-query_end, query_length-query_start)` for `-`. A left-target
patch uses the oriented prefix before that interval; a right-target patch uses
the oriented suffix after it. The selected source read end is evidence only and
must agree with this normalized orientation.

When both ends of one contig are patched, compute both cut coordinates first
and require the retained interval to be non-negative. An overlapping or
internally inconsistent pair is not materialized automatically. Preserve it as
review evidence with a clear reason. Patch materialization is atomic for the
assembly: if any selected contig has conflicting cuts, no patched FASTA is
published, even when other selected contigs would be individually valid.

Patch records must report the actual target bases removed and the actual bases
added. `extension_length` alone is not sufficient evidence for replay.

### No-read artifacts

The no-read path writes its empty TSV files under `stage_work_dir(...)`, then
publishes them with `publish_or_recover_work_artifact(...)` using the same
artifact types as the ordinary path. Returned stage refs must point at
immutable published artifacts and must be present in the artifact index.

## Implementation Surface

- `app/schemas/run_config.py`
- `app/entrypoints/ask_planner.py`
- `app/entrypoints/run.py`
- `app/storage/materialization.py`
- `app/stages/t2t/nodes.py`
- `app/schemas/artifacts.py`
- T2T planner descriptors and examples

Do not add an automatic patch safety-floor validator in this task. The existing
operator mode and the later human gates own whether a candidate is accepted.
This task is limited to correct input enumeration and correct sequence
materialization for an already selected candidate.

## Failure Behavior

- A malformed member of the canonical ONT list blocks the stage as an input
  problem; it is not silently skipped.
- Duplicate read identity across assets must either be namespaced
  deterministically or fail before alignment.
- Invalid junction coordinates, an empty replacement tail, or overlapping
  left/right cuts leave the candidate unmaterialized and preserve an explicit
  reason.
- No branch may mutate the source assembly.

## Tests

- Two registered clean-read files are both scanned.
- Multiple relative run-config paths are materialized independently and retain
  input order.
- Clean reads take precedence over raw reads without selecting only one clean
  asset.
- Duplicate read IDs cannot overwrite one another silently.
- A right-end candidate with `target_distance_bp > 0` trims the target suffix
  at `target_end` before adding the tail.
- A left-end candidate with `target_distance_bp > 0` trims the target prefix at
  `target_start` before adding the tail.
- Compatible two-ended patches use one retained target interval.
- Conflicting cut coordinates do not create a patched FASTA.
- No-read TSVs originate in work storage, are published immutably, and are
  registered.
- The original input FASTA remains byte-for-byte unchanged.

## Acceptance Criteria

- [x] T2T consumes the complete canonical ONT read list.
- [x] Run-config materialization supports and normalizes the list field.
- [x] Read evidence remains attributable to its source asset.
- [x] Patch materialization uses PAF junction coordinates.
- [x] Patch records include target trimming evidence.
- [x] Conflicting junctions cannot be silently materialized.
- [x] No-read artifacts use work-to-immutable publication.
- [x] `T2TFinishingBundle` exposes `ont_read_refs` and no singular read ref.
- [x] Unit and stage tests cover multi-file reads and non-zero target distance.
