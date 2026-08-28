# NCBI reference snapshots

This page is the sole operator authority for the NCBI reference root and its
installed taxonomy snapshots. Deployment guides link here instead of copying
the contract.

The active `envs.yaml` registers one machine-local root:

```yaml
databases:
  ncbi:
    reference_dir: /home/lux/databases/ncbi
```

Each immediate child directory is one independently verified snapshot. The
initial animal and plant layout is:

```text
databases/ncbi/
├── metazoa/
│   ├── Metazoa.ncbi.tsv
│   ├── Metazoa.ncbi.lineage
│   ├── species_alias.tsv
│   └── reference_manifest.json
└── viridiplantae/
    ├── Viridiplantae.ncbi.tsv
    ├── Viridiplantae.ncbi.lineage
    ├── species_alias.tsv
    └── reference_manifest.json
```

The eight files may be placed directly in this layout. Each manifest must
declare exactly its same-prefix `*.ncbi.tsv`, same-prefix `*.ncbi.lineage`, and
`species_alias.tsv`, including the exact byte size and SHA-256 of each file.
Extra files are not runtime inputs.

Alternatively, install one four-file directory or ZIP through the helper:

```bash
python scripts/install_ncbi_reference.py \
  --reference metazoa \
  --source-dir /shared/reference/metazoa

python scripts/install_ncbi_reference.py \
  --reference viridiplantae \
  --source-url "$GENE_AGENT_NCBI_VIRIDIPLANTAE_SNAPSHOT_URL"
```

The helper writes to `<reference_dir>/<reference>` and validates the included
manifest. Verify either installed snapshot at any time with:

```bash
python scripts/install_ncbi_reference.py --reference metazoa --verify-only
python scripts/install_ncbi_reference.py --reference viridiplantae --verify-only
```

At run readiness, the sole manifest-owned `target_species` is tested against
every installed snapshot using the existing fallback order: exact species,
same genus, same family, then same order. The snapshot providing the best
available tier is selected; equal tiers prefer more primary records and then
the snapshot directory name for deterministic behavior. Readiness fails only
when no snapshot provides useful evidence. The freeze records every installed
manifest hash so replacing any snapshot cannot silently alter a committed run.
