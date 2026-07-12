# s41467-025-65778-w — cattle T2T candidate-comparison methods note

## Applicable scenario

The study seeks “the most contiguous genome assembly” with the greatest number of T2T chromosomes for a diploid cattle sample with HiFi, standard ONT, ultra-long ONT, parental, and Hi-C data. Ten candidate assemblies compare Verkko, hifiasm, HERRO-corrected ONT, ONT coverage, and Verkko `-C0` versus `-C2`.

## Reported selected workflow

ONT reads were filtered to at least 10 kb and average quality Q10 before HERRO correction. The selected input balance was approximately `58.1×` HiFi, `57×` HERRO-corrected ONT, and `121×` ONT overall; `18.3×` came from reads longer than 100 kb.

The publication identifies the following key Verkko form rather than a complete reusable command:

```bash
verkko \
  -C 0 \
  --hifi <PacBio_HiFi> <HERRO_corrected_ONT> \
  --nano <ONT_and_UL_ONT> \
  <parent_hapmers_and_other_options>
```

HiFi polishing reads were aligned with `minimap2 -a -asm20`.

## Interpretation and transfer notes

- `57×` HERRO plus `-C0` produced the most T2T chromosomes. Relative to `-C2`, it yielded longer telomeres and more T2T chromosomes, with slightly lower QV and slightly higher switch error.
- Raising HERRO-corrected ONT to `99×`, beyond the HiFi coverage, made the assembly more fragmented.
- `-C0`, `--hifi`, and `--nano` are Verkko options, and minimap2 is downstream polishing. They are not hifiasm L2 parameters.
- The hifiasm comparison is retained as corpus context, but no unreported hifiasm command or value is reconstructed.
