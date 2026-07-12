# s41586-022-05325-5 — hifiasm trio methods note

## Applicable scenario

The study constructs a high-quality diploid human reference genome. Its conclusion includes that “graph-based haplotype phasing during assembly outperformed those that did not”. The workflow uses HiFi reads and parental short reads for trio phasing, with Hi-C/Omni-C and ONT ultra-long data supporting later reference-grade finishing. The case records `read_type: hifi` and target method `hifiasm` as scoring attributes. Trio and auxiliary-data availability remain documentary context rather than discrete retrieval conditions.

## Why Trio hifiasm was used

Parent-specific k-mers derived from parental short reads are combined with the HiFi assembly graph to separate paternal and maternal haplotypes. ONT ultra-long sequence was subsequently used to bridge GA-rich repeats and other complex gaps between HiFi contigs.

## Explicitly reported Trio hifiasm test

The paper reports parental k-mer counting:

```bash
yak count \
  -b37 \
  -o sr-pat.yak \
  sr-pat.fq.gz

yak count \
  -b37 \
  -o sr-mat.yak \
  sr-mat.fq.gz
```

It then reports the following hifiasm test command:

```bash
hifiasm \
  -1 sr-pat.yak \
  -2 sr-mat.yak \
  hifi-reads.fq.gz
```

This command corresponds to asm9 and hifiasm `v0.3`. The final reference assembly used hifiasm `v0.14.1` in trio mode, but the paper does **not** provide the complete final invocation. That missing command must not be reconstructed.

## Downstream ONT-UL gap-filling context

After haplotype binning, ONT ultra-long reads were assembled with Flye:

```bash
flye \
  --threads 128 \
  --min-overlap 10000 \
  --asm-coverage 40 \
  --out-dir <MOM-or-DAD> \
  --genome-size 3.1g \
  --nano-raw haplotype-<DAD-or-MOM>.fasta.gz
```

The final workflow used Flye `v2.8.3-b1695` and explicitly retained `--min-overlap 10000`. ONT contigs were subsequently corrected with Medaka and Merfin before gap filling.

## Transfer notes

- The yak options and hifiasm `-1`/`-2` inputs document trio setup; they are not mapped to hifiasm catalog L2 parameters here.
- Flye, Medaka, and Merfin belong to downstream gap filling and polishing. Their settings must not be represented as hifiasm parameter evidence.
- hifiasm `v0.14.1` is recorded as the final workflow version, while the only complete hifiasm command preserved above is explicitly labeled as the earlier `v0.3` test.
