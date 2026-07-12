# s41597-024-03664-8 — sorghum T2T methods note

## Applicable scenario

The sorghum genome is approximately `725 Mb`, with about `0.10%` heterozygosity and `67.74%` repetitive sequence. The workflow targets a T2T, gap-free plant assembly by combining PacBio HiFi, ONT, and Hi-C data.

## Reported preprocessing and assembly

CCS generation used:

```bash
ccs \
  --minPasses 3 \
  --minPredictedAccuracy 0.99 \
  --minLength 500 \
  subreads.bam \
  HiFi.bam
```

Hi-C quality control used SOAPnuke with the reported `-n 0.01`, `-l 20`, `-q 0.1`, `-i`, `-Q 2`, `-G 2`, `-M 2`, and `-A 0.5` settings.

hifiasm `0.19.5` was run with default parameters to produce initial contigs. The paper does not publish a complete nondefault hifiasm command.

## Downstream workflow

- Hi-C reads pass through BWA and HiC-Pro, then Juicer and 3D-DNA for chromosome scaffolding.
- ONT reads are corrected by NECAT and supplied to LR_Gapcloser.
- HiFi reads are aligned with Winnowmap2, clipped alignments are filtered, and Racon performs polishing.

## Transfer notes

The default-parameter statement documents the absence of reported hifiasm overrides; it is not evidence for pinning a historical default value. CCS, SOAPnuke, NECAT, LR_Gapcloser, Winnowmap2, and Racon settings belong to upstream or downstream tools and are not hifiasm L2 parameter evidence.
