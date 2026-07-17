# ADR 0004 — Object verification precedes materialization

- Status: **Accepted for implementation v0**
- Date: **2026-07-17**

## Context

Materialization must never consume an object merely because a file exists at
the expected path. Existence, format labels, and filenames do not establish
identity.

The project also distinguishes verification from manifest regeneration.

## Decision

Before an object can be copied or extracted, its recorded digest must be
verified.

The first implementation:

- supports SHA-256 only;
- verifies regular files only;
- streams data in bounded chunks;
- rejects symlinks and non-regular files;
- detects metadata changes during the read;
- returns mismatch without altering the baseline;
- treats optional recorded size as an additional constraint;
- uses exit code `1` for a completed mismatch and `2` for an invalid or unsafe
  verification request.

Directory objects remain unsupported until the project defines one canonical,
portable serialization and hashing procedure.

## Consequences

- `ogv materialize` can later require successful verification receipts.
- Corrupt or substituted runner and bottle archives fail before extraction.
- Verification remains independent from rebuilding manifests.
- Existing directory-format declarations cannot yet be consumed as verified
  immutable objects.
