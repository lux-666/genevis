# NCBI Metazoa reference snapshot

This page is the sole operator authority for acquiring and installing this
snapshot. Deployment guides link here instead of copying commands.

The large NCBI exports are intentionally not committed. The tracked
`reference_manifest.json` pins the runtime TSV/lineage snapshot by source
metadata, byte size, and SHA-256; `species_alias.tsv` is also tracked and
pinned. `Metazoa.ncbi.json` may be kept beside them for offline inspection but
is not read by the sanity-check node and is therefore not part of its runtime
verification cost.

The preferred server command downloads one published ZIP and installs it into
the `databases.ncbi_metazoa.reference_dir` selected by the active `envs.yaml`:

```bash
python scripts/install_ncbi_metazoa_reference.py \
  --source-url "$GENE_AGENT_NCBI_SNAPSHOT_URL"
```

The ZIP must contain these four files at its root:
`reference_manifest.json`, `Metazoa.ncbi.tsv`, `Metazoa.ncbi.lineage`, and
`species_alias.tsv`. The installer accepts it only when the included manifest
exactly matches the repository-pinned manifest and all sizes and SHA-256 hashes
match. `GENE_AGENT_NCBI_SNAPSHOT_URL` is a shell variable for the deployment's
published artifact URL; it is not application configuration.

For an offline/shared-filesystem installation, use the same complete snapshot:

```bash
python scripts/install_ncbi_metazoa_reference.py \
  --source-dir /shared/reference/ncbi_metazoa
```

Verify the configured destination at any time with:

```bash
python scripts/install_ncbi_metazoa_reference.py --verify-only
```

A changed NCBI export must receive a new manifest (including its actual export
timestamp and source version) in the same commit as the alias table or code
that consumes it, then be published as one four-file ZIP. Do not distribute
the TSV, lineage, alias table, and manifest independently.
