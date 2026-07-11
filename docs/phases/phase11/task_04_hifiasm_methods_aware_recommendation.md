# Task 4: Hifiasm Methods-Aware Evidence Recommendation

**Phase**: 11
**Status**: Structure contract and conservative prompt policy implemented; weak/no-evidence behavioral acceptance pending Task 6; retry evidence pending Task 5; cutover pending Task 6
**Depends on**: Task 2 EvidenceCase contract; Task 3 numeric retrieval
**Created**: 2026-07-10
**Updated**: 2026-07-11

## Goal

Use the Task 3 hifiasm numeric shortlist as the only source of curated Methods
evidence for parameter recommendation, while keeping source provenance,
prompt contents, model input, response validation, and replay artifacts bounded
and auditable.

## Implemented scope

Task 4 implements:

- a Methods-aware response contract with separate case assessments and L2
  parameter decisions;
- a closed hifiasm prompt projection;
- a single canonical sanitized prompt per recommendation attempt;
- deterministic case, span, excerpt, and serialized-byte budgets;
- exact-quote handling: a quote is copied in full or the whole span is omitted;
- deterministic bounded omission metadata;
- response validation against only prompt-visible cases, spans, profile facts,
  and structured retry signals;
- fail-closed handling for SKILL heuristics until a structured heuristic
  registry exists;
- compatibility projection into the existing `params` and
  `parameter_evidence` fields used by catalog/rule validation.

Task 4 does **not** enable production cutover. The real knowledge-pack strategy
keeps `production_cutover_enabled: false`. Task 3 gate tests exercise the
numeric path only through a temporary test pack with the gate enabled.

## Ownership boundaries

### Task 2 owns the nested EvidenceCase schema; Task 3 owns ranking provenance

The Task 2 EvidenceCase module contains the only implementation of the nested
hifiasm prompt schema. Its public
`project_hifiasm_prompt_safe_case(...)` boundary accepts only Task 2
`validated` cases, accepts no caller-supplied `numeric_match`, and returns a
non-ranked `prompt_base` object. Task 3 alone performs the private one-way
`validated -> ranked` transition. Task 4 accepts only that Task 3-owned ranked
kind and uses the same shared nested-field projector rather than maintaining a
second schema.

Task 4 owns only:

- validation of Task 3's ordered ranked-selection envelope before reading raw
  shortlist IDs, ordering, or retrieval metadata;
- case/span/character/byte budgets;
- removal of references to omitted spans;
- prompt-level closed projection;
- Methods-aware response normalization and citation checks.

The shared Task 2 field projection drops `source_documents` and other
non-prompt fields, validates bounded text, and rejects unknown nested fields.
Task 3 attaches `numeric_match` while minting the ranked case. Task 4 verifies
that ranked proof and copies the bound numeric metadata; it cannot add, replace,
or re-attest caller-supplied numeric metadata.

The private Task 3 selection envelope binds ordered candidate, selected, and
evidence IDs; selected case identity and order; every ranked case's complete
semantic content and `numeric_match`; eligible/total counts; normalized query;
warning; and the ranking trace. Task 4 first compares the caller-visible audit
fields with that registered envelope and materializes retrieval query/trace
artifacts only from the attested projection. A changed trace distance, appended
candidate, replaced warning/query, or forged eligible count therefore fails
before either retrieval artifact is written. Task 4 then verifies every
supplied evidence entry before applying any visibility or serialized-byte
budget. Thus even a trailing case that will be recorded only as an omission
must still be a genuine Task 3-ranked case. The private envelope itself is not
copied into the canonical prompt.

### No caller-list mutation

Evidence cases are rebuilt into new dictionaries and new lists. Task 4 does not
truncate, reorder, or otherwise modify the selector-owned input list in place.

## Single canonical prompt flow

For a gated hifiasm numeric attempt, the prompt builder constructs the canonical
prompt exactly once:

```text
build_prompt_payload(...)
  -> validate session/attempt components
  -> build profile, Task 3 selection audit, evidence, and raw prompt in memory
  -> require the complete attested Task 3 shortlist prefix
  -> sanitize_hifiasm_methods_prompt_payload(...) once in the prompt builder
  -> compute prompt_payload_hash from canonical content
     excluding prompt_payload_ref and prompt_payload_hash
  -> bind the exact final prompt_payload_ref/hash in a final attestation
  -> serialize and validate every planned artifact in memory
  -> publish profile, retrieval, evidence, and canonical prompt artifacts
  -> return that same final canonical dictionary to the assembly node
  -> pass the same canonical dictionary to AssemblyDecisionRequest
  -> serialize that dictionary as the model user JSON
  -> use that dictionary for model-unavailable fallback
  -> use that dictionary for Methods response validation
  -> use that dictionary for downstream deterministic parameter validation
```

There is no pre-sanitization `prompt_payload.json`, and no artifact is published
until profile projection, Task 3 envelope/audit validation, complete-prefix
validation, Task 2/3 case provenance validation, Task 4 budgeting, canonical
prompt validation, final ref/hash sealing, and serialization of the whole
artifact plan have all succeeded. A validation or preparation failure therefore
leaves zero attempt artifacts. If publication itself fails after an earlier
planned artifact was atomically published, a same-content retry resumes through
the writer's idempotent no-clobber behavior; no raw or partially written prompt
artifact is ever exposed. The assembly node no longer performs a second
sanitize or prompt write.

All attempt-scoped JSON artifacts use the same immutable writer. The writer
first validates the `session_id` as one safe path component and `attempt_id` as
an exact non-boolean integer at least zero. It walks from a trusted root
directory descriptor with `openat`-style `dir_fd`, `O_DIRECTORY`, and
`O_NOFOLLOW` operations, retaining the directory descriptors and inode
identities for the complete chain. Each newly created child directory is
followed by an `fsync` of its parent. The writer then serializes to UTF-8 bytes
and constructs the SHA-256 and `ArtifactRef` before publication. Those exact
bytes are written to a unique same-directory temporary file, flushed, and
file-`fsync`ed.

Publication uses an atomic hard link to the final path, never `os.replace(...)`:

- the first writer publishes without clobbering an existing attempt artifact;
- a repeated or concurrent writer with identical bytes succeeds idempotently;
- a repeated or concurrent writer with different bytes fails explicitly;
- a successful new publication does not reread or rehash the shared final path;
- an existing final entry is opened with `O_NOFOLLOW`, required to be a regular
  file by `fstat`, and compared through that descriptor;
- the success ordering is file `fsync` -> final hard link -> attempt-directory
  `fsync` -> temporary unlink -> attempt-directory `fsync`;
- the root-anchored namespace chain is verified immediately before publication
  and again after the durable cleanup sequence. If an attempt directory is
  renamed/replaced during publication, the writer removes a newly linked final
  through the retained attempt descriptor, syncs the rollback, and fails rather
  than returning a path reference to the displaced directory.

Directory `fsync` failures reported as unsupported by the operating system or
filesystem are handled by the documented compatibility no-op; other failures
propagate. Advisory locks (if added by a deployment) cannot fully isolate the
writer from an arbitrary malicious process that already has write permission
to the artifact namespace. Directory ownership and permissions remain the
security boundary. The concrete rename/replacement race above is nevertheless
detected and fails closed.

Temporary-file and directory-descriptor cleanup runs from a `finally` path and
does not replace an active `KeyboardInterrupt`, `SystemExit`, or other original
exception. Thus a failed/interrupted call leaves no partial final JSON and no
temporary residue; an already published final path remains immutable.

`AssemblyDecisionRequest` has a required, out-of-band `prompt_contract` with no
default. Its only values are `legacy` and `hifiasm_methods_v1`. The assembly node
chooses that contract before prompt construction from the trusted selected
method, task, and knowledge-pack cutover configuration; it never infers the
contract from mutable prompt markers, backend names, evidence fields, or hashes.
Route selection, retries pending Task 5, non-hifiasm methods, and the default
gate-off path explicitly dispatch as `legacy`.

The request preserves the exact final canonical dictionary object instead of
asking Pydantic to rebuild it as `dict[str, Any]`, which would discard the
private attestation. Construction snapshots the validated explicit contract in
private request state. Therefore later mutation of the public contract field,
or removal of every in-band recognizer from a no-case prompt, cannot downgrade a
`hifiasm_methods_v1` dispatch. That contract unconditionally requires the final
registry proof at request construction and again before model serialization,
fallback, and response validation, including verification of the exact
`prompt_payload_ref` and `prompt_payload_hash`. The contract is control-plane
state only: it is not inserted into the replay prompt or model user JSON. A
plain gate-off structured-default prompt remains compatible because its node
dispatch is explicitly `legacy`, not because the prompt happens to lack a Task
4 marker. Neither `make_assembly_decision(...)` nor
`normalize_hifiasm_methods_response(...)` sanitizes, reprojects, or rebudgets
the prompt. A JSON-roundtripped prompt, or an in-process mutation followed by a
recomputed self-hash, therefore fails closed before the affected boundary.

The canonical hash is:

```python
generate_payload_ref(
    {
        key: value
        for key, value in canonical_prompt.items()
        if key not in {"prompt_payload_ref", "prompt_payload_hash"}
    }
)
```

End-to-end tests assert that the replay `prompt_payload.json`, the model user
JSON, and the canonical omission metadata are equal, including a non-empty
`omitted_spans` list, and that the stored hash matches the canonical payload.

## Canonical envelope and idempotence

Canonical hifiasm prompts carry
`hifiasm_methods_prompt_schema_version: 1`, but neither that marker nor a
payload-provided hash is a trust anchor. Initial construction first validates
Task 3's ranked-selection envelope and every supplied ranked case, before
reading raw IDs for budgeting or omission metadata. Task 4 then records a draft
semantic proof in a private weak-reference registry associated with the exact
canonical prompt object. After the canonical semantic hash and replay path are
known, a private sealing step creates the final attested object whose proof
binds the exact final ref/hash as well as all prompt content. Draft prompts are
not accepted at the decision boundary. The proofs are not stored in
caller-writable JSON and controlled copy/deepcopy operations preserve them.

A marked input is accepted for sanitizer re-entry only when that external
attestation exists and still matches the complete semantic prompt. It is then
strictly checked for:

- the closed top-level field set and `selected_cases: []`;
- exact equality of `case_selection` and the allowlisted `case_retrieval`;
- shortlist ordering and visible evidence-case IDs;
- the Task 3-ranked cases that were anchored during initial construction,
  including bound numeric metadata and cleaned visible span references;
- fixed Methods contract fields;
- exact budget limits, deterministic omitted-case entries, bounded and typed
  omitted-span entries, recomputed usage, and serialized-byte compliance;
- canonical self-reference field types and, when present, the canonical hash;
- fail-closed removal of `skill_heuristic` from the prompt policy.

After that validation, the canonical dictionary is copied without rebuilding
or rebudgeting it. Therefore prompts with omissions satisfy:

```text
sanitize(sanitize(raw)) == sanitize(raw)
```

The canonical hash, `omitted_cases`, `omitted_spans`, and already-cleaned span
references remain stable across repeated sanitation. Converting a canonical
prompt to ordinary JSON/dict state intentionally loses the in-memory
attestation: replay JSON is an audit artifact, not an input that can self-upgrade
through the public sanitizer. Recomputing a matching payload hash after changing
a quote or forging omission metadata still fails before canonical re-entry.

## Closed hifiasm prompt boundary

The canonical hifiasm prompt is rebuilt from an explicit top-level allowlist.
It does not deepcopy the original prompt and then patch selected fields.

The projection:

- forces `selected_cases: []` in curated hifiasm mode;
- rebuilds `case_retrieval` from explicit scalar, ID-list, artifact-ref, hash,
  and gate metadata fields;
- derives `case_selection` from that same safe retrieval projection;
- rebuilds `evidence_cases` from envelope-validated Task 3 ranked cases through
  the shared Task 2 nested schema and Task 4 budgets;
- rebuilds `methods_evidence` from fixed contract text, fixed effect areas, and
  generated budget audit metadata;
- drops unknown top-level containers;
- drops raw retrieval `query`, `trace`, and `debug_blob` values;
- drops caller-supplied or alternate `methods_evidence` content;
- drops `source_documents` and full source text;
- drops unstructured `retry_evidence` until Task 5 defines and populates its
  prompt-safe structure.

This is a closed projection: alternate containers such as `selected_cases`,
raw retrieval debug fields, top-level source documents, or caller-supplied
Methods metadata cannot bypass the evidence-case allowlist.

## Evidence budgets

Budgets are applied in stable numeric-shortlist and source-span order:

| Budget | Limit | Overflow behavior |
|---|---:|---|
| visible cases | 8 | omit cases after the leading eight |
| visible spans per case | 12 | omit the whole excess span |
| excerpt characters per case | 6,000 | omit the whole span that would exceed the case budget |
| excerpt characters across cases | 24,000 | omit the whole span that would exceed the total budget |
| serialized evidence payload | 65,536 UTF-8 bytes | repeatedly omit the tail visible case until the payload fits |

Quotes are never content-truncated. After a span is omitted, all nested
`evidence_span_ids` are recursively filtered so they reference only spans still
visible in that case.

The evidence budget records:

```yaml
limits:
  cases: 8
  spans_per_case: 12
  excerpt_chars_per_case: 6000
  total_excerpt_chars: 24000
  serialized_bytes: 65536
used:
  cases: 0
  spans: 0
  excerpt_chars: 0
  serialized_bytes: 0
case_usage: []
omitted_cases:
  - case_id: case_09
    reason: case_limit | serialized_evidence_bytes
omitted_spans:
  - case_id: case_01
    span_id: methods.example
    reason: span_limit_per_case | excerpt_chars_per_case | excerpt_chars_total
```

Audit metadata is included in the serialized-byte calculation. Ordering is
deterministic. If byte enforcement removes a case, audit entries belonging to
that no-longer-visible case are removed, the omitted case is recorded, and
usage is recomputed to a fixed point.

## Response contract

The model returns four top-level fields:

```yaml
case_assessments: []
recommendation:
  adjustable_params: {}
selected_case_ids: []
summary: "..."
```

Validation requires:

- assessments reference only `case_retrieval.evidence_case_ids`;
- transferable findings use a fixed Methods effect area and cite only spans
  visible in the same case;
- selected cases have an accepted assessment;
- every visible L2 parameter has exactly one `set`, `keep_default`, or `omit`
  decision;
- `basis=case` cites selected accepted cases and visible spans;
- `basis=profile_fact` cites a path present in `prompt_profile`;
- `basis=catalog_default` remains valid without a case;
- retry citations use only structured signal IDs or paths actually visible in
  the canonical prompt;
- values still pass the existing catalog and hard-rule validator.

Weak or absent case evidence is not a structural blocker. A no-case response
may use `profile_fact` and `catalog_default`. Behavioral acceptance of weak and
no-evidence scenarios remains pending Task 6.

## Structured heuristic policy

The current real parameter-recommender pack has hard validation rules in
`rules.rules[]`, but it does not have a structured prompt-visible SKILL
heuristic registry. Hard-rule IDs are not heuristic IDs and must not be reused
as `heuristic_labels`.

Therefore Task 4 fails closed:

- `skill_heuristic` is removed from the canonical hifiasm prompt's allowed
  evidence bases;
- any response using `basis=skill_heuristic` or any non-empty
  `heuristic_labels` is rejected;
- real-pack tests verify that a hard validation rule ID cannot be presented as
  a SKILL heuristic ID.

**Structured heuristic IDs are pending.** Task 4 does not introduce a new
registry.

## Retry policy

`retry_signals` may cite only structured retry signal IDs or paths that are
actually visible in the canonical prompt. The current Task 4 projection does
not admit free-form retry logs or inferred failure text. Until Task 5 defines
and populates the structured retry evidence block, `basis=retry_failure` fails
closed.

## Artifacts and compact state

A gated numeric attempt writes replay artifacts including:

```text
data/artifacts/assembly/<session>/parameter_attempt_<n>/
├── input_profile.json
├── retrieval_query.json
├── retrieval_trace.json
├── evidence_payload.json
├── prompt_payload.json
├── llm_response.json
└── validation.json
```

The compact `ParameterAttempt` stores refs, candidate IDs, selected IDs,
validation results, case assessments, parameter decisions, and the
recommendation summary. Full prompt evidence remains ref-backed.

## Test coverage

Task 2/3/4 and neighboring prompt-builder tests cover:

- Task 2-valid case fixtures produced through source-bundle validation and the
  Task 3-owned ranked transition;
- approved validated cases failing if sent directly to Task 4, and ranked
  cases rejecting public Task 2 re-projection or numeric replacement;
- full-chain rejection of reordered candidate/evidence IDs, swapped ranked
  cases, empty or shortened prefixes, appended unattested candidates, and
  ordinary-dict tail entries that would otherwise be omitted by the case budget;
- legitimate ranked selections surviving copy/deepcopy, including no-case
  output;
- exact excerpts without full source documents;
- no mutation of caller-owned case values;
- unknown nested case fields failing closed;
- replacement-container and debug-field leakage prevention;
- all case/span/character/serialized-byte budgets;
- whole-span omission without quote truncation;
- deterministic omission IDs, reasons, ordering, and usage;
- prompt-visible case/span/profile citation validation;
- hard validation rule IDs rejected as heuristic IDs;
- retry basis failing closed without Task 5 structured evidence;
- valid no-case `profile_fact` and `catalog_default` decisions;
- replay prompt, model user JSON, canonical hash, and omission metadata equality;
- non-empty `omitted_spans` in the end-to-end replay/model/hash assertion;
- canonical sanitizer idempotence and strict rejection of forged envelopes;
- quote and omission-audit forgery failing even with a recomputed matching hash;
- malformed selection/case/canonical input failing before publication and
  leaving zero attempt artifacts;
- interruption before canonical publication leaving no pre-clean prompt artifact;
- final ref/hash mutation or deletion failing the decision proof before model use;
- explicit `hifiasm_methods_v1` dispatch rejecting a no-case JSON roundtrip after
  every schema/evidence/retrieval recognizer is removed, both backends are
  changed to `structured_default`, and the payload hash is recomputed;
- explicit `legacy` and default gate-off dispatch remaining compatible, with
  `prompt_contract` absent from replay and model user JSON;
- first-byte partial I/O failure preserving the previous complete artifact and
  removing the temporary file;
- immutable no-clobber publication under interleaved identical and different
  concurrent writers;
- safe single-component session paths plus resolved symlink containment;
- `KeyboardInterrupt`/`SystemExit` cleanup without temporary residue;
- pre-publication hash/reference validation and file-sync, publish, directory-
  sync ordering;
- Task 3 temporary gate-on integration and default production gate-off behavior.

## Acceptance status

- [x] Structure contract implemented.
- [x] Conservative prompt policy implemented.
- [x] Task 2 nested case schema reused; Task 4 accepts only Task 3-ranked cases
  and maintains no duplicate EvidenceCase schema.
- [x] Canonical prompt is constructed once and reused by replay, model,
  fallback, and validators.
- [x] Canonical prompt artifact is written once, only after sanitize and hash.
- [x] Versioned canonical input is strictly validated and omission-idempotent.
- [x] Canonical re-entry requires validation-owned external attestation; marker
  and self-computed hash are not trust anchors.
- [x] Decision dispatch uses a required out-of-band contract; Methods-aware
  requests cannot be downgraded by deleting or rewriting prompt content.
- [x] JSON artifacts use flushed/synced same-directory temp files, immutable
  no-clobber hard-link publication, and parent-directory durability sync.
- [x] Closed prompt boundary blocks alternate evidence/debug containers.
- [x] Evidence budgets and bounded deterministic audit metadata implemented.
- [x] Hard validation rule IDs cannot masquerade as SKILL heuristic IDs.
- [ ] Structured heuristic IDs are available. Pending future pack design.
- [ ] Task 5 supplies structured retry evidence and end-to-end retry citations.
- [ ] Task 6 accepts weak/no-evidence model behavior.
- [ ] Task 6 enables production cutover.
