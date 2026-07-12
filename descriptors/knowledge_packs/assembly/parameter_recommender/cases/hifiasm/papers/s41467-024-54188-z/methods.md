# s41467-024-54188-z — male goat T2T methods note

## Applicable scenario

The study targets a telomere-to-telomere assembly of a male goat using “ultralong ONT and PacBio HiFi reads together with Hi-C and Bionano data”. Parental short reads additionally support trio binning. This is relevant to large diploid animal genomes where centromeres, telomeres, sex chromosomes, and other complex repeats must be resolved.

## Reported commands

The authors' repository reports hifiasm `0.16.1` for the initial HiFi assembly:

```bash
hifiasm \
  -o IMCG-204112 \
  -t 40 \
  <HiFi_1.fa> <HiFi_2.fa> <HiFi_3.fa> <HiFi_4.fa>
```

Parent-specific k-mers were counted with yak:

```bash
yak count -k 23 -b 37 -t 20 -o IMCGF.yak <father-R1.fastq.gz> <father-R2.fastq.gz>
yak count -k 23 -b 37 -t 20 -o IMCGM.yak <mother-R1.fastq.gz> <mother-R2.fastq.gz>
```

The trio assembly was then run as:

```bash
hifiasm \
  -o rsy \
  -t 30 \
  -1 IMCGF.yak \
  -2 IMCGM.yak \
  offspring.hifi.fa.gz
```

## Workflow and transfer notes

- HiFi produces the initial assembly; Bionano and Hi-C organize it at chromosome scale.
- Parent-specific k-mers support trio binning, and binned ONT or HiFi data support gap filling and haplotype assembly.
- `-1` and `-2` are trio-route inputs. Output prefixes, threads, paths, and yak preprocessing options are not hifiasm L2 parameter recommendations.
- No unreported finishing command or adjustable hifiasm value is reconstructed.
