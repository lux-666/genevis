# s41587-022-01261-x — hifiasm methods note

## Applicable scenario

The reported use case is a diploid sample with PacBio HiFi and Hi-C data but no parental sequencing. The paper describes the goal as to “produce a haplotype-resolved assembly without the sequencing of parents”. The case records `read_type: hifi` and target method `hifiasm` as scoring attributes. Hi-C is retained as method context and is not a discrete retrieval condition.

## Why this workflow was used

The paper treats stable haplotype assembly from a single sample as difficult. HiFi reads provide a high-accuracy assembly graph, while Hi-C supplies chromosome-scale haplotype contacts that support phasing without parent-specific data.

## Reported commands

Supplementary Information reports hifiasm `0.16.1-r375`:

```bash
hifiasm \
  -o <outputPrefix> \
  --h1 <HiC-reads-R1.fasta> \
  --h2 <HiC-reads-R2.fasta> \
  -t <nThreads> \
  <HiFi-reads.fasta>
```

A HiFi-only invocation is also reported for partial phasing:

```bash
hifiasm \
  -o <outputPrefix> \
  -t <nThreads> \
  <HiFi-reads.fasta>
```

## Transfer notes

- `--h1` and `--h2` describe selected Hi-C inputs; this case does not turn their presence into an L2 parameter recommendation.
- Thread count, output prefix, and input paths are operational placeholders, not transferable biological parameter evidence.
- The HiFi-only command documents a reduced phasing scenario and should not be interpreted as producing the same phasing result as the HiFi plus Hi-C workflow.
