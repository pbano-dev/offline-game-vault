# ADR 0010: generic persistent-state transactions

Status: accepted
Date: 2026-07-18

## Context

The Dark Souls Remastered pilot proved preservation and restoration of a save
and gbe_fork identity through host-local scripts. Those scripts encoded a
specific game, bottle layout, and operator workflow.

Persistent state must instead be declared by each capsule and handled by a
generic transaction that can be reused by Bottles, Wine, UMU, and future
Windows adapters.

The immutable vault, a materialization, a mutable execution deployment, and a
state backup are different objects. Treating any one of them as another would
destroy provenance or make cleanup unsafe.

## Decision

The orchestrator provides:

```text
ogv audit-capsule
ogv preserve-state
ogv verify-state-backup
ogv restore-state
```

All state paths are relative to an explicit state root. The orchestrator never
guesses that root from a title, AppID, username, or adapter.

A state backup is a private directory with:

- payloads copied under opaque state-ID containers;
- one manifest entry for every regular file and directory;
- SHA-256 for every regular file;
- a deterministic per-item tree digest;
- original POSIX modes;
- a state-definition digest;
- a strict receipt.

Normal capture rejects a missing required item. A pre-restore snapshot records
missing items so rollback can reproduce absence as well as content.

Backup publication is staged and no-replace. Restore stages each replacement,
takes a mandatory verified snapshot, checks live state for changes, verifies
the installed result, and rolls back touched items on failure.

Symlinks, special files, and multiply linked regular files are rejected. They
require a future explicit preservation policy rather than implicit copying.

Public sanitized fixtures can pass structural audit but cannot be used for
capture or restore.

## Consequences

Positive:

- game-specific backup scripts can be retired after their private capsule is
  migrated and functionally revalidated;
- backup and restore receipts are machine-readable and schema-validated;
- no absolute host paths are written to receipts or command output;
- a restore failure has an explicit rollback artifact and status;
- the same state engine can be called by multiple execution adapters.

Costs and limitations:

- operators must supply the correct state root;
- operators must stop all writers and explicitly confirm that condition;
- backups are private directories, not yet immutable vault objects;
- cross-user identity portability remains a separate acceptance test;
- state trees requiring symlinks or hard links are unsupported in generation
  `0`.

## Alternatives rejected

### Continue game-specific scripts

Rejected because path knowledge, error handling, and privacy behavior would
diverge for every title.

### Store mutable saves inside the immutable bottle object

Rejected because gameplay would mutate the archive baseline and make object
verification meaningless.

### Restore without a mandatory snapshot

Rejected because a partial multi-item restore would have no reproducible
rollback source.

### Follow symlinks

Rejected because a symlink can escape the declared state root and copy or
overwrite unrelated host data.
