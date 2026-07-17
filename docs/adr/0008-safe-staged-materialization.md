# ADR 0008 — Verified staged materialization

- Status: **Accepted for Linux pilot v0**
- Date: **2026-07-17**

## Context

Materialization consumes immutable objects but creates mutable host-local
trees. A failed extraction must not expose a partial destination, and archive
member names cannot be trusted merely because the archive digest is known.

Runner archives may legitimately contain internal symlinks. Rejecting every
symlink would destroy the runner; following unrestricted links would be unsafe.

## Decision

Materialization uses:

```text
verify profile
→ create sibling staging directory
→ safe per-object extraction
→ verify source objects again
→ write receipt
→ atomic no-replace promotion
```

Tar policy:

- reject absolute member paths;
- reject `..` traversal and backslash separators;
- reject duplicate destinations and special files;
- preserve regular files and executable modes;
- preserve only relative symlinks whose normalized targets stay inside the
  object extraction root;
- preserve hardlinks only to already extracted regular files;
- never overwrite a staging path;
- fsync regular files and directories before promotion.

On Linux, directory promotion uses `renameat2(RENAME_NOREPLACE)`. On Windows,
the implementation relies on the platform's non-replacing rename behavior.
Other systems fail rather than using a non-atomic fallback.

Removal:

- requires the local receipt;
- refuses unknown top-level paths;
- refuses declared persistent state unless preservation has been confirmed;
- atomically detaches the destination before recursive deletion.

## Consequences

- A canonical destination is either absent or complete.
- Unsafe archive structure fails before publication.
- Exact runner symlinks can survive materialization.
- A crash may leave a clearly named sibling staging directory, never a partial
  canonical destination.
- The first pilot does not yet back up mutable state automatically.
