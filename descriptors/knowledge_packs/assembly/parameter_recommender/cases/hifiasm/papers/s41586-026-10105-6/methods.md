# s41586-026-10105-6 — hifiasm ONT methods note

## Applicable scenario

The reported scenario uses “ONT R10.4.1 standard simplex reads without ultra-long sequencing”: standard ONT simplex reads are available, but neither HiFi data nor a dedicated ultra-long library is required. The goal is a partially phased near telomere-to-telomere assembly. This is the sole `read_type: ont` paper case in the initial corpus and targets `hifiasm_ont`; read type and target method remain independent scoring attributes rather than hard filters.

## Why the ONT-specific workflow was used

The same ONT sequencing error can recur at the same position in multiple reads. A correction approach that assumes mostly random errors can mistake these recurrent errors for true heterozygous sites. The reported hifiasm ONT approach uses read phasing to distinguish variants that maintain a consistent local phase from recurrent errors that lack stable phase relationships.

## Reported command

Supplementary Information reports hifiasm `0.25.0-r726`:

```bash
hifiasm \
  -o <outputPrefix> \
  -t <nThreads> \
  --ont \
  --dual-scaf \
  --telo-m CCCTAA \
  <ONT-reads.fastq>
```

## Parameter ownership and transfer notes

- `--ont` activates the ONT simplex route. It is route-owned L0 behavior, not an L2 recommendation.
- `--dual-scaf` is represented by the catalog L2 parameter `dual_scaffold: true` because it is explicitly present in the reported command.
- `CCCTAA` is the human telomere motif used in this command. Other species must not copy it without taxon-specific support. The motif is therefore retained as documentary evidence but is not exposed as transferable `telomere_motif` parameter evidence.
- Output prefix, thread count, and read path are operational values rather than paper-derived parameter recommendations.
