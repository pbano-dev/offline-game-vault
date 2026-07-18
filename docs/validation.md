# Repository validation

Run locally from the repository root:

```bash
python -m pip install -r requirements-ci.txt
python tools/validate_repository.py
```

The validator checks:

- all JSON Schemas, including vault inventory and persistent-state receipts, are valid Draft 2020-12 schemas;
- each fixture `capsule.json` validates;
- host contracts, acceptance reports, and receipts validate;
- referenced documents and metadata files exist;
- profile dependencies reference existing object IDs;
- profile and object IDs are unique;
- host-contract platforms match their profiles;
- verified profiles have passing acceptance evidence;
- orphan contracts and acceptance reports are rejected;
- common absolute private-path patterns are rejected in fixture JSON.

This validation does not verify archived payload hashes because commercial
payloads are outside Git.

The unit suite also exercises safe tar extraction, staged materialization, receipt validation, and guarded removal.

## Bottles adapter validation

The unit suite also verifies:

- a materialized bottle is copied rather than modified in place;
- existing managed bottle names are never overwritten;
- source and staged trees match before identity rewriting;
- unsafe or broken symlinks and special files are rejected;
- `Name`, `Path`, and `Custom_Path` are rewritten only in the derivative;
- deployment receipts validate against the Bottles deployment schema;
- launch plans do not disclose the private managed path;
- isolated profiles include `flatpak run --unshare=network`;
- removal requires stopped and persistent-state confirmations.


## Persistent-state validation

The unit suite also verifies:

- operational and sanitized capsule audit outcomes;
- safe non-overlapping state paths;
- mandatory stopped-process confirmation;
- private `0700`/`0600` backup permissions;
- regular-file and directory capture;
- required and optional missing state;
- SHA-256 and tree-manifest verification;
- payload-tampering detection;
- rejection of symlinks and multiple hard links;
- mandatory pre-restore snapshots;
- restored live-state verification;
- rollback after an injected multi-item restore failure;
- backup and restore receipts against their Draft 2020-12 schemas;
- sanitized CLI and receipt output without absolute host paths.
