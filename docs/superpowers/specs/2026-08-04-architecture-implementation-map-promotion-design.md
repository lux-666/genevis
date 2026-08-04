# Architecture Implementation Map Promotion Design

**Date:** 2026-08-04
**Status:** approved in conversation; ready for specification review

## Objective

Promote the maintained LangGraph implementation map from the task-oriented
`topics/` directory to the architecture root because it spans every topic,
reference contract, stage graph, and registered method.

The new canonical path is:

`docs/architecture/langgraph-implementation-map.md`

The move must preserve all current user edits in the implementation map and
must not include those edits or unrelated working-tree changes in the migration
commit. After the commit, the user edits remain as unstaged modifications at
the new path so they can be committed atomically with their corresponding code,
tests, references, and conformance changes.

## Documentation hierarchy

The reading path becomes:

```text
docs/README.md
  -> docs/architecture/README.md
     -> topics/system-map.md for system ownership
     -> langgraph-implementation-map.md for implementation maturity
        -> topics/<subsystem>.md for behavioral explanation
           -> reference/<contract>.md for exact fields and invariants
```

The implementation map is not a topic or a reference-contract owner. It is a
cross-cutting, evidence-backed projection at the same level as the architecture
router.

The change-synchronization path is distinct from the reading path:

```text
code or behavior change
  -> update the unique owning reference contract
  -> update each affected topic
  -> update the implementation map when wiring, status, evidence, or debt changes
  -> update README/router only when navigation relationships change
```

The two README routers and the implementation map itself must state that a
graph node, canonical stage, registered method, execution adapter, collection
boundary, parameter flow, human gate, resource commit, verification
classification, or cited conformance change requires a synchronized
implementation-map update in the same change. Topic and reference pages remain
unchanged unless they contain a direct live link to the old path.

## File changes

1. Move with history:
   `docs/architecture/topics/langgraph-implementation-map.md` to
   `docs/architecture/langgraph-implementation-map.md`.
2. Repair the moved document's relative links:
   topic links gain `topics/`; reference links change from `../reference/` to
   `reference/`.
3. Update `docs/README.md` to link to the architecture-root map and explain the
   reading and synchronization paths.
4. Update `docs/architecture/README.md` to treat the map as a sibling
   architecture index rather than a topic-router member while still exposing
   it prominently.
5. Update current non-historical references to the old path. Historical design
   specifications may retain the path they originally specified unless they
   contain a live navigation link.

## Commit and working-tree handling

The migration commit contains the committed `HEAD` version of the map at the
new path, with only the relative-link repairs required by its new directory.
It does not absorb the map's current unstaged implementation-status edits.

The working tree after the commit contains the same user-edited map content at
the new path, with the same relative-link repairs applied. Consequently the new
path remains modified relative to the migration commit, while the old path is
fully removed. This is intentional and must be reported to the user.

The staged index must be inspected before commit. It may contain only:

- the old-path deletion/new-path addition or detected rename for the committed
  baseline map;
- `docs/README.md` navigation and synchronization text;
- `docs/architecture/README.md` navigation and synchronization text.

The migration implementation may use an explicit index-only reconstruction of
the committed map baseline when normal `git add` would stage the user's existing
map edits. It must not reset, restore, or overwrite the working-tree version.

## Scope controls

- Do not change the implementation classifications or overwrite the user's
  current additions to the map; keep them unstaged after the migration commit.
- Do not modify production code, tests, descriptors, reference contract
  content, or conformance conclusions as part of this path migration.
- Do not commit unrelated dirty-worktree files.
- Do not create a second implementation-map stub at the old path; one canonical
  document avoids two sources of truth.

## Verification

The migration is complete when:

- the old file path no longer exists;
- every current navigation link resolves to the new path;
- every relative link inside the moved map resolves;
- repository search finds no live old-path reference outside historical design
  prose;
- the staged commit contains only the committed-baseline map move/link repair
  and directly affected README navigation files;
- the user's pre-existing map content changes remain visible as an unstaged
  modification at the new path after commit;
- `git diff --cached --check` succeeds.
