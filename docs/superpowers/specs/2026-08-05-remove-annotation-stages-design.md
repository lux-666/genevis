# Remove Annotation Stages From the Current Product

## Decision

Remove `repeat_annotation`, `gene_annotation`, and `function_annotation` from
the current working tree. Git history is the only retention mechanism. Do not
leave hidden, disabled, placeholder, compatibility, or archived copies in the
repository.

The current product has ten ordered stages:

```text
inventory
read_qc
genome_survey
assembly
assembly_dedup
assembly_review
hic_scaffolding
gapcloser
t2t
base_polish
```

`base_polish` is the final parent-graph stage before `done`. GapCloser and Base
Polish retain their existing manual-only placeholder contracts.

## Removal Boundary

Delete the three stage packages and the annotation-intake workflow used to
replan and enter them. Remove their parent-graph nodes, stage names, public
RunConfig fields, TUI and Ask planning behavior, review gates, report logic,
resource handoffs, artifact schemas, descriptors, package/environment entries,
examples, deployment instructions, and stage-focused tests.

Delete annotation-only manifest and registry asset types:

- `repeat_library`
- `repeat_annotation`
- `softmasked_genome`
- `gene_models`
- `predicted_proteins`
- `transcripts_fasta`
- `function_database`
- `function_annotation`

Retain sequencing and evidence asset types that still participate in current
upstream behavior, including RNA-seq, long-RNA/Iso-Seq, and
`reference_proteins`. Retain BUSCO descriptors and environment configuration
because Assembly Review still executes BUSCO.

## Runtime And Data Behavior

`STAGE_ORDER`, `DomainStage`, `RequestableStage`, and `LocalExecStage` must not
contain any removed stage. The parent graph must neither import nor compile
their builders. Public run configuration, interactive planning, automatic
recommendation, dependency freezing, review resume, inspect, and reporting must
have no annotation-stage branch.

Existing run configs, manifests, or checkpoints containing removed stage or
asset names are unsupported and may fail strict validation. No migration,
deprecated enum, tombstone stage, or legacy checkpoint reader will be added.

The logical resource chain ends with the adopted `assembly`; remove
`masked_assembly` and `predicted_proteins` workflow resources. Generic artifact,
ledger, execution, asset-registry, and resume infrastructure remains unchanged
except for deleting annotation-specific projections or hooks.

## Documentation And Examples

Update `docs/architecture` as the authoritative current contract:

- describe the ten-stage product boundary;
- end stage contracts and resource handoff at Base Polish;
- remove annotation nodes, methods, maturity claims, and counts from the
  implementation map;
- remove annotation-intake plan mutation from runtime and review topics.

Delete annotation-specific examples. Delete the obsolete E2E manifest/run-config
pair instead of rewriting it into a duplicate of the focused current examples.
Remove current README and deployment claims that require RepeatMasker,
AUGUSTUS, Miniprot, gffread, EVM, or DIAMOND.

Historical superseded material may be deleted from the working tree when its
only purpose is the removed annotation product. Git history remains the record.
Do not rewrite unrelated archival or design material merely because it contains
the English word “annotation”.

## Verification

Delete stage-specific test modules. Update shared tests that used removed stage
names as generic fixtures, stage-order expectations, descriptor counts, T2T
handoffs, run-config examples, or report examples.

Acceptance requires:

1. public schemas reject all three removed stages and annotation-only assets;
2. the parent graph contains no annotation stage node;
3. planners and TUI never recommend or display them;
4. the descriptor registry contains only current methods and packages;
5. architecture, README, deployment docs, and examples contain no current
   product claim for the removed chain;
6. focused configuration, routing, planner, manifest, inventory, resume,
   reporting, storage, and T2T tests complete successfully;
7. the full test suite completes successfully;
8. a final repository scan finds no production reference to the removed stage
   identifiers, except an explicitly reviewed historical context that remains
   useful and does not claim current support.

Unrelated dirty-worktree changes must be preserved, particularly the current
changes under `.agents/disabled-skills/`, `AGENTS.md`,
`app/entrypoints/run.py`, `app/stages/inventory/nodes.py`,
`tests/test_configuration_examples.py`, `tests/test_inventory.py`, and
`tests/test_run_command.py`.
