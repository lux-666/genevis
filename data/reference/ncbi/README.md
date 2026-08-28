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

Runtime selection and BUSCO lineage resolution are owned by the
[Assembly Review stage contract](../../../docs/architecture/reference/stage-contracts.md#6-assembly-review).
