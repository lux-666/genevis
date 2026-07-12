# s41588-025-02137-x — hexaploid wheat T2T methods note

## Applicable scenario

The study addresses a roughly `15 Gb` hexaploid bread-wheat genome with more than `85%` repetitive sequence. Inputs include approximately `150×` HiFi, `100×` ultra-long ONT, Bionano optical maps, and Hi-C. This is a scale- and repeat-intensive polyploid T2T scenario.

## SPART candidate assemblies

The authors' SPART repository calls three assemblers independently:

```bash
SPART/00_Contig_screen/hifiasm.sh \
  $HiFi_reads $ONT_reads $output_prefix $threads

SPART/00_Contig_screen/verkko.sh \
  $output_prefix $HiFi_reads $ONT_reads $threads $memory

SPART/00_Contig_screen/flye.sh \
  $ONT_reads $output_prefix $threads
```

hifiasm produces the principal contigs. Verkko and Flye provide independent validation, gap patches, and telomere repairs. Bionano supports scaffolding, while Hi-C supports chromosome placement and short-contig cleanup.

## Reported patching and telomere polishing

SPART patches the target assembly from candidate assemblies with RagTag:

```bash
ragtag.py patch \
  -i 0.99 \
  --remove-small \
  -q 10 \
  -u \
  --aligner minimap2 \
  -t 128 \
  --mm2-params "-x asm20 -I1G -t 128" \
  reference.fasta query.fasta
```

Telomeric ONT consensus is generated with Medaka, then HiFi reads are aligned with minimap2 and Racon performs correction.

## Transfer notes

The published hifiasm wrapper exposes only input reads, output prefix, and thread count. It does not expose a defensible adjustable hifiasm value. Verkko, Flye, RagTag, Medaka, minimap2, Racon, Bionano, and Hi-C settings remain multi-assembler or downstream context and are not hifiasm L2 parameter evidence.
