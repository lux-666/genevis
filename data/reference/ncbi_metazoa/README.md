# NCBI Metazoa reference snapshot

The large NCBI exports are intentionally not committed. The tracked
`reference_manifest.json` pins the runtime TSV/lineage snapshot by source
metadata, byte size, and SHA-256; `species_alias.tsv` is also tracked and
pinned. `Metazoa.ncbi.json` may be kept beside them for offline inspection but
is not read by the sanity-check node and is therefore not part of its runtime
verification cost.

Before an assembly-review run on the HPC checkout, populate this directory
with the ignored TSV and lineage snapshot files and verify them:

```bash
python scripts/install_ncbi_metazoa_reference.py \
  --source-dir /shared/reference/ncbi_metazoa
```

If the files have already been copied into this directory, run:

```bash
python scripts/install_ncbi_metazoa_reference.py --verify-only
```

The installer never downloads data. A changed NCBI export must receive a new
manifest (including its actual export timestamp and source version) in the
same commit as the code or alias-table update that consumes it.
