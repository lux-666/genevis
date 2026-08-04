# TUI Midstage Entry

> **Status:** planned; not available in the current TUI.  
> **Read when:** implementing or discussing a TUI run that starts from cleaned
> long reads or an existing assembly.  
> **Comes from:** the current [TUI and planning](tui-and-planning.md) boundary
> and backend support for registered manifest assets.  
> **Feeds into:** whole-run freeze, [runtime orchestration](runtime-orchestration.md),
> Assembly, or Assembly Review.  
> **Contract dependencies:** [stage contracts](../reference/stage-contracts.md),
> [resource handoff](../reference/resource-handoff.md), and
> [run data](../reference/data-contract.md).  
> **Code evidence:** current gap in `app/schemas/tui_intake.py` and
> `app/entrypoints/tui_intake.py`; backend capability in
> `app/schemas/manifest.py`, `app/stages/inventory/`, and existing-assembly
> integration tests.

## Accepted entry modes

| Entry mode | TUI fact | Manifest asset | Earliest normal stage |
|---|---|---|---|
| `raw_reads` | Existing HiFi/ONT intake | `raw_long_reads` | Existing behavior |
| `clean_long_reads` | One or more cleaned long-read files plus explicit `ont` or `pacbio_hifi` platform | `clean_long_reads` | `assembly` |
| `existing_assembly` | Exactly one readable assembly FASTA | `assembly_fasta_registered` | `assembly_review` |

The backend already understands both asset types and can route an existing
assembly through inventory. The current TUI does not expose an `entry_mode`,
clean-read fact, or existing-assembly fact. This page is therefore an accepted
target, not evidence that the feature works today.

## Intended intake boundary

Explicit wording selects a midstage mode; a generic request to “do assembly”
does not. Existing-assembly intent has priority over clean-read intent when
both are stated. The selected mode changes the deterministic requirement queue:

- raw mode retains the current HiFi/ONT/Hi-C questions;
- clean-read mode asks only for cleaned reads, platform, and any required
  planning facts such as an operator genome-size estimate;
- existing-assembly mode asks only for one assembly FASTA.

Paths remain exact user-supplied files. The TUI does not scan directories,
guess ONT versus HiFi from a filename, choose among several assemblies, or
create an arbitrary-stage importer. Invalid, unreadable, empty, ambiguous, or
multi-assembly input remains unresolved rather than being guessed.

## Planning and freeze

The accepted mode and confirmed asset are persisted in the mutable draft and
materialized through the existing manifest models. The planner may begin clean
reads at Assembly or an existing assembly at Assembly Review; it must not
reinsert earlier stages merely because they precede that stage in the global
order. The operator may still edit the Plan Draft, but normal stage input
contracts continue to reject an impossible plan.

Final confirmation uses the same whole-run freeze as every other TUI run. The
feature must not introduce another executor, unchecked path override, second
manifest format, or bypass around Inventory and the artifact/resource model.

## Completion boundary

Implementation is complete only when the typed draft, deterministic and LLM
intent parsing, requirement queue, manifest projection, plan generation,
resume/revision behavior, user documentation, and focused tests all agree.
Until then, current operator documentation must continue to state that these
two entry modes are unavailable in the conversational TUI.
