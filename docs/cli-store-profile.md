# Profile object-store workflow

## Ingest a complete profile

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli ingest-profile   --capsule fixtures/<game>/capsule.json   --profile <profile-id>   --vault-root <vault-root>   --source '<object-id>=<source-file>'   --source '<object-id>=<source-file>'
```

Properties:

- every absent dependency requires an explicit source;
- source IDs outside the selected profile are rejected;
- assignments are checked before any object is copied;
- source files are verified during copying;
- canonical destinations are never overwritten;
- existing matching objects return `already_present`;
- reports omit host source and destination paths;
- rerunning the command is idempotent.

A failure in a later object may leave earlier objects already ingested. Those
objects are complete and verified; no partial canonical object is published.

## Verify a profile

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli verify-profile   --capsule fixtures/<game>/capsule.json   --profile <profile-id>   --vault-root <vault-root>   --json
```

Exit status:

```text
0  every dependency verified
1  one or more dependencies missing or mismatching
2  invalid metadata, unsafe path, or verification error
```

## Generate a deterministic inventory

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli inventory   --vault-root <vault-root>   --output <vault-root>/VAULT_INVENTORY.json
```

The inventory:

- contains no timestamp or absolute path;
- hashes every canonical object;
- is sorted by digest;
- records exact byte counts;
- is written atomically outside `objects/`;
- remains byte-identical while the vault is unchanged.

Raw command lines can contain source paths. Do not archive shell history or
unsanitized terminal logs.
