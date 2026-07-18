# CLI: generic persistent-state management

Version introduced: `0.7.0`.

These commands turn the capsule `persistent_state` declaration into a
verified private backup and restore workflow. No game-specific path is
hard-coded in the orchestrator.

## Capsule audit

```bash
ogv audit-capsule \
  --capsule <CAPSULE>
```

Machine-readable output:

```bash
ogv audit-capsule \
  --capsule <CAPSULE> \
  --json
```

The audit checks, among other things:

- capsule, object, profile, and state IDs;
- canonical object-store paths;
- document and profile references;
- unknown profile dependencies;
- safe relative persistent-state paths;
- duplicate or overlapping state paths;
- public redactions and unresolved placeholders.

A sanitized public fixture can be structurally valid while remaining
non-operational. Replace its redactions only in a private capsule.

`audit-capsule` is a runtime structural audit. Repository CI still performs
the authoritative JSON Schema and fixture cross-reference validation.

## State root

Every `persistent_state[].path` is resolved below the explicit
`--state-root`.

For a Bottles deployment, this is normally the mutable derived bottle
directory that directly contains `drive_c/`. It is not automatically the
materialization root, the managed Bottles directory, or the vault root.

The command rejects:

- absolute and non-canonical declared paths;
- `..` traversal;
- symlinks in a declared state path or tree;
- regular files with multiple hard links;
- sockets, devices, FIFOs, and other special files;
- overlapping persistent-state declarations.

The state root must be an existing contained directory. Filesystem roots and
the user's home directory are not valid state roots.

## Capture state

First stop the game, Wine/Bottles/UMU processes, and any other writer of the
declared state. Then run:

```bash
ogv preserve-state \
  --capsule <PRIVATE_CAPSULE> \
  --state-root <STATE_ROOT> \
  --backup <NEW_PRIVATE_BACKUP_DIRECTORY> \
  --confirm-stopped
```

The destination must not exist.

The command:

1. audits the private capsule;
2. resolves all entries with `backup=true`;
3. rejects missing entries whose `required` value is `true`;
4. stages a private copy;
5. hashes every regular file;
6. detects source changes during capture;
7. writes `state-backup.json`;
8. publishes the complete directory without overwrite;
9. verifies the published backup again.

Permissions created by the command:

```text
backup directories: 0700
backup payload files: 0600
state-backup.json: 0600
```

Original file and directory modes are recorded in the private receipt and are
reapplied during restoration.

## Verify a backup

```bash
ogv verify-state-backup \
  --capsule <PRIVATE_CAPSULE> \
  --backup <PRIVATE_BACKUP_DIRECTORY>
```

The verifier checks:

- private permissions;
- capsule ID;
- persistent-state definition digest;
- item IDs, paths, kinds, sensitivity, and required status;
- payload paths and types;
- SHA-256 for every file;
- byte and member counts;
- per-item tree digests;
- absence of unexpected payload containers;
- absence of symlinks, hard links, and special files.

A backup is bound to the state definition, not to the capsule's formatting or
unrelated documentation.

## Restore state

Stop all writers first:

```bash
ogv restore-state \
  --capsule <PRIVATE_CAPSULE> \
  --state-root <STATE_ROOT> \
  --backup <VERIFIED_PRIVATE_BACKUP> \
  --snapshot <NEW_PRE_RESTORE_SNAPSHOT_DIRECTORY> \
  --confirm-stopped
```

The snapshot destination must not exist.

The command:

1. verifies the restore source;
2. creates and verifies a mandatory private snapshot of current state;
3. verifies that live state has not changed since the snapshot;
4. stages each replacement beside its target;
5. swaps each item only after its staged copy verifies;
6. verifies the restored live state;
7. writes `state-restore-receipt.json` into the snapshot.

If any item fails, every item already touched is restored from the mandatory
snapshot. The receipt records one of:

```text
completed
rolled_back
rollback_failed
```

When rollback is incomplete, stop immediately and retain the snapshot.

## Backup layout

```text
<PRIVATE_BACKUP>/
├── state-backup.json
└── payload/
    ├── 0000-<STATE_ID>/
    │   └── data
    └── ...
```

A pre-restore snapshot can additionally contain:

```text
state-restore-receipt.json
```

Receipts contain capsule-relative state paths because they are needed for
restoration. They never contain the absolute host state root or backup path.

## Scope and limitations

`--confirm-stopped` is an explicit operator assertion. The generic command
does not discover every possible game, Wine, Bottles, UMU, cloud-sync, or
editor process.

This workflow preserves mutable state. It does not:

- prove network isolation;
- modify the immutable game or runner objects;
- ingest private backups into the immutable object store;
- make a public sanitized fixture operational;
- prove cross-user or cross-host portability.

Private backups remain private artifacts. Archive and replicate them under the
collection's normal integrity and redundancy policy.
