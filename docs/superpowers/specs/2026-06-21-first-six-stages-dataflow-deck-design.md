# First Six Stages: Dataflow Deck Design

## Purpose

Create a Chinese PowerPoint deck that makes the first six executable workflow
stages understandable at a glance, emphasizing the actual control flow and
biological-data lineage implemented in this repository.  It is a visual
reference for the author as well as a presentation for a supervisor.

## Scope

The deck covers the real, wired stages in this order:

`inventory -> read_qc -> genome_survey -> assembly -> assembly_review -> hic_scaffolding`

It excludes planned-but-unwired features such as `t2t`, and it does not cover
the downstream repeat, gene, or function annotation stages.

## Narrative

The deck explains a single promise: large biological files travel as registered
artifacts, while the LangGraph state carries only compact references,
decisions, and recovery facts.  Each stage validates inputs, chooses a path,
creates or registers outputs, then returns control to the thin parent router.

## Slide Plan

1. **Title** — “Gene Agent：前六个 Stage 的逻辑与真实数据流”.
2. **Parent flow and legend** — six stages in order; legend distinguishes
   control flow, artifact/data flow, optional/skipped branches, and review
   gates.
3. **Inventory** — manifest validation and asset registration.  Outputs:
   `asset_registry_ref`; ambiguous classifications reach a review gate.
4. **Read QC** — `raw_long_reads` through SeqKit statistics and optional
   length filtering to `clean_long_reads` plus FOFN and report artifacts.
5. **Genome survey** — branch on `clean_short_dna_reads`: Jellyfish and
   GenomeScope2 create `genome_survey_result`; otherwise preserve an explicit
   long-read prior / skipped result.
6. **Assembly** — input profiling and route selection feed a candidate plan;
   Flye or NextDenovo method subgraphs emit `draft_assembly.candidate` bundles.
7. **Assembly review** — candidate assemblies feed QUAST and BUSCO, then rule
   evaluation, comparison, and the mandatory human decision gate; the selected
   output becomes `final_assembly_for_scaffolding_or_annotation`.
8. **Hi-C scaffolding** — target-level and Hi-C asset checks select Juicer +
   3D-DNA or YaHS, yielding scaffolded assembly, AGP, QC, and review bundle;
   absent data or a non-chromosome target leads to an explicit skip.
9. **End-to-end lineage** — a consolidated data rail from manifest/FASTQ to
   final assembly, with the separate responsibilities of checkpoint state,
   artifact registry, and ledger.

## Visual Language

- 16:9 deck, dark navy background, pale canvas, and high-contrast
  orange/teal/violet accents.
- Every detailed stage slide has three lanes: control nodes along the top,
  artifact/data nodes along the bottom, and branch/review exits on the right.
- Solid arrows mean control flow; thick colored arrows mean artifact lineage;
  dashed arrows mean optional or skip paths; a diamond denotes a routing
  condition; a red outlined octagon denotes a human gate.
- Node labels use actual builder node names where space permits, paired with a
  short Chinese explanation. Artifact labels use their actual registered type
  or state contract.

## Fidelity Rules

- Stage order and top-level wiring must match `app/graph/builder.py` and
  `app/stages/__init__.py`.
- Detailed nodes must match the respective `app/stages/<stage>/builder.py`.
- Output asset names and stage boundaries must follow `docs/stage_contracts.md`
  and `app/schemas/artifacts.py`.
- The diagrams may simplify layout, but must not imply unsupported execution,
  parallelism, or a data dependency absent from the implementation.

## Verification

- Render every slide and inspect at readable size.
- Ensure no text overlaps, clips, wraps unexpectedly, or obscures arrows.
- Check every stage page has a visible entry condition, success output, and
  skip/fail/review outcome where implemented.
- Include speaker-friendly one-line takeaways, but do not rely on paragraphs
  to explain the figure.
