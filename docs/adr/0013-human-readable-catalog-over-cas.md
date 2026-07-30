# ADR 0013: human-readable catalog over content-addressed storage

- Status: accepted
- Date: 2026-07-30
- Schema generation: 0

## Context

A SHA-256 content-addressed store gives stable identity and deduplication but is
not legible in a graphical file browser. Physically separating objects into
game, runner, runtime, and backend directories would make category part of the
address and would require moving unchanged content when metadata changes.

## Decision

Keep immutable object paths based only on SHA-256.

Add a regenerable `00_CATALOG` view containing `.ogvref` JSON references:

```text
GAMES/
SHARED/
BY_DIGEST/
UNREFERENCED/
```

A reference records digest, object ID, role, kind, scope, size, physical CAS
path, and consumers. It is not a copy, hardlink, or authority.

`UNREFERENCED` means that no current canonical capsule references the object.
It does not mean the object is safe to delete.

## Consequences

- CAS identity and deduplication remain stable.
- File-browser navigation becomes useful.
- Classification can change without moving content.
- Catalog loss is recoverable from capsules and the CAS.
- Deletion requires a separate receipt-aware reachability audit.
- Core catalog commands and `.ogvref` schema validation remain implementation
  work.
