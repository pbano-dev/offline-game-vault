# ADR 0005 — Content-addressed, no-overwrite ingestion

- Status: **Accepted for implementation v0**
- Date: **2026-07-17**

## Context

The archive requires one shared store for runners, bottle archives, runtimes,
tools, and other immutable objects. Filenames and package locations are not
stable identities.

Ingestion modifies the vault, so partial writes and accidental replacement of
an existing object must be prevented.

## Decision

Objects are stored at:

```text
objects/sha256/aa/bb/<full-sha256>
```

where `aa` and `bb` are the first two byte pairs of the SHA-256 digest.

The ingestion sequence is:

```text
source
→ copy to same-directory temporary file while hashing
→ fsync temporary file
→ atomic no-overwrite promotion
→ verify stored destination
```

Rules:

- never move or modify the source;
- never overwrite a destination;
- reject a pre-existing mismatching destination;
- reject source symlinks and non-regular files;
- require canonical capsule `archive_path`;
- remove failed temporary files;
- return `already_present` for a verified existing object.

The first implementation uses a same-filesystem hard link for atomic
no-overwrite promotion. Unsupported filesystems fail explicitly.

## Consequences

- Shared dependencies can be stored once.
- An interrupted copy does not expose a partial canonical object.
- Hash identity determines storage, not user-provided filenames.
- Optical and portable exports can resolve the same object graph.
- Ingestion requires a filesystem that supports the atomic promotion method.
