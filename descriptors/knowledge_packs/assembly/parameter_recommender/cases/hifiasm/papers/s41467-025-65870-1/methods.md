# s41467-025-65870-1 — single-insect T2T methods note

## Applicable scenario

The study assembles a single male oriental fruit fly under constraints of “small body size, poor conservation of telomere and centromere structures, high levels of heterozygosity”. The low-input HiFi library is accompanied by substantial microbial contamination. ONT reads from an inbred population provide additional long-range support.

## Read cleanup

The authors used hifiasm and HiCanu graph structure together with microbial databases to identify contaminant contigs, then removed the reads that composed those contigs. ONT reads were cropped and only reads longer than 80 kb were retained:

```bash
NanoFilt \
  --headcrop 50 \
  --tailcrop 50 \
  < ONT.fastq \
  > ONT.cropped.fastq
```

The long retained ONT reads were used to validate and extend telomere, centromere, and other repeat-rich regions.

## Reported hifiasm command

The paper reports hifiasm `r603`:

```bash
hifiasm \
  -o <prefix> \
  --hg-size 540m \
  --primary \
  -r 5 \
  -D 6 \
  -m 1000000 \
  --b-cov 2 \
  -l 1 \
  -O 2 \
  clean_HiFi.fastq.gz
```

## Downstream and transfer notes

The primary assembly was followed by Purge_Dups `1.2.6`, Juicer `1.6`, 3D-DNA `2.20`, and manual Juicebox adjustment.

- `--hg-size 540m` records the sample's genome-size prior and remains L1/read-only.
- `--primary` is route/output behavior and remains L0.
- The explicitly reported L2 values map to `correction_rounds`, `high_freq_kmer_drop`, `contig_bubble_pop_distance`, `break_low_coverage`, `purge_level`, and `duplicate_overlap_min_reads`.
- NanoFilt thresholds and downstream-tool versions are not hifiasm parameter values.
