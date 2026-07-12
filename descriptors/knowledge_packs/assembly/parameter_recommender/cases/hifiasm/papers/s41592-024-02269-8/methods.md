# s41592-024-02269-8 — hifiasm methods note

## Applicable scenario

The paper targets “population-wide near telomere-to-telomere assemblies” by combining PacBio HiFi and ONT ultra-long reads for scalable assembly of diploid or polyploid genomes. Hi-C or parental data can additionally support phasing. The case records `read_type: hifi` and target method `hifiasm` as scoring attributes. UL-ONT and Hi-C remain documentary context rather than discrete retrieval conditions.

## Why this workflow was used

HiFi reads provide high sequence accuracy, but their length may be insufficient to bridge centromeres, long repeats, and segmental duplications. ONT ultra-long reads provide long-range paths through those regions. The workflow combines these complementary properties.

## Reported preprocessing

The paper filters short ultra-long reads to “avoid assembly errors and reduce the running time”:

```bash
# HPRC Year 2
seqkit seq -m100000 <ultra-long-reads.fasta>

# HPRC Year 1 and plant samples
seqkit seq -m50000 <ultra-long-reads.fasta>
```

These are `seqkit` preprocessing thresholds. They are **not** hifiasm `--ul-cut` values and must not be mapped to the catalog parameter `ul_cut`.

## Reported hifiasm command

For HiFi + UL-ONT + Hi-C, hifiasm `0.19.5-r587` is reported as:

```bash
hifiasm \
  -o <outputPrefix> \
  -t <nThreads> \
  --h1 <HiC-reads-R1.fasta> \
  --h2 <HiC-reads-R2.fasta> \
  --hom-cov <homozygous-coverage> \
  --ul <ultra-long-reads.fasta> \
  <HiFi-reads.fasta>
```

## Transfer notes

- `--hom-cov` is filled from the sample’s homozygous coverage peak; it is not a fixed paper-derived value. In this project `hom_cov` is L1/read-only and is not exposed as adjustable L2 evidence.
- `--ul`, `--h1`, and `--h2` identify selected input data. Their presence is recorded as methods context only and does not create a topology filter.
- The two `seqkit -m` cutoffs belong to different cohorts and sample classes and must not be generalized into one universal cutoff.
