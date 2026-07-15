# s41588-025-02273-4 — pear gap-free assembly methods note

## Applicable scenario

The study reports that “high heterozygosity and technical limitations result in gaps within reference genomes”. The two pear cultivars have heterozygosity estimates of `1.23%` and `1.33%`. The workflow is therefore relevant to diploid plant samples where the initial haplotypes remain interrupted by difficult sequence.

## Reported workflow

- hifiasm `0.19.0` uses PacBio HiFi, ONT ultra-long, and Hi-C inputs to generate two initial haplotype assemblies.
- Verkko `1.4.1` provides sequences capable of bridging gaps in the hifiasm result.
- HiCanu contributes telomeric sequence.
- ALLHiC `0.9.8` and Hi-C data scaffold contigs to chromosomes, followed by manual correction.

## Transfer notes

The paper publishes the software versions, data combinations, and role of each assembler, but not a complete one-line hifiasm command. This case therefore records no hifiasm L2 parameter value. Verkko, HiCanu, ALLHiC, and manual finishing remain downstream or cross-assembler context rather than hifiasm parameter evidence.
