# Task 3: Hifiasm EvidenceCase Scoring

**Phase**: 11
**Status**: Implemented for initial recommendation; verification pending
**Depends on**: Task 2 EvidenceCase v3 contract
**Updated**: 2026-07-12

## Goal

Rank approved initial hifiasm-family EvidenceCases with one deterministic,
missing-aware score. Case attributes influence ranking but do not create a
separate routing topology.

## Query

The canonical query retains:

- `method_id`;
- normalized `read_type` (`hifi`, `ont`, or missing);
- numeric genome/sample features;
- clade as a final tie-break context.

Hi-C, UL-ONT, Trio, and `assembly_goal` are not query dimensions in this task.

## Eligibility Versus Similarity

Only non-scenario validity checks exclude a case:

- curation status is approved;
- source validation marked it retrieval-eligible;
- case type is initial.

Method identity and read type are ordinary similarity attributes. A mismatch or
missing query value lowers the score but does not remove the candidate. This
keeps the current corpus continuous; a future configuration can approximate
method routing simply by assigning method identity an extremely large weight.

## Score

For numeric feature `i`, distance contributes only when both query and case have
a usable value:

```text
D_numeric = sum(w_i * present_i * robust(d_i)) /
            sum(w_i * present_i)

C_evidence = sum(w_i * present_i) / sum(w_i)
S_numeric = C_evidence * exp(-D_numeric)
```

When no numeric feature is comparable, numeric distance remains `null` and
`S_numeric` is zero. Missing values are never converted to zero.

Current categorical contributions are:

```text
S_method    = 1 when target method matches, else 0
S_read_type = 1 when read type matches, else 0
```

The single ranking score is:

```text
S_total = S_method + S_read_type + S_numeric
```

Candidates sort by:

1. `S_total` descending;
2. evidence coverage descending;
3. numeric distance ascending;
4. clade tie-break;
5. case ID.

The initial selector retains up to five cases. The ranking trace records method
match, read-type match, categorical score, numeric fields, and total score.

## Method Naming

The hifiasm EvidenceCase backend accepts both method families:

- `hifiasm`;
- `hifiasm_ont`, with `hifiasm-ont` normalized as the same identity.

The wider assembly catalog still contains Flye and NextDenovo, but this task
does not add their paper cases or a new backend.

## Acceptance Criteria

- [x] Method/read-type mismatch does not hard-filter a valid case.
- [x] Method and read type affect one unified score.
- [x] Numeric distance and coverage remain missing-aware.
- [x] `hifiasm_ont` and `hifiasm-ont` normalize to one identity.
- [x] Hi-C, UL-ONT, Trio, topology, and assembly goal do not filter or score.
- [x] Ranking and selected IDs remain attested for downstream replay.
- [x] Retry tasks do not invoke initial EvidenceCase selection.
