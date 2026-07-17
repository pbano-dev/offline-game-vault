# `ogv ingest-object`

`ogv ingest-object` verifies a regular source file and copies it into the
content-addressed object store.

## Capsule mode

```bash
ogv ingest-object   --source /path/to/backup.tar.gz   --capsule fixtures/dark-souls-remastered/capsule.json   --object-id dsr-bottle-baseline   --vault-root /path/to/vault
```

The capsule must declare the canonical path:

```text
objects/sha256/<first-two>/<next-two>/<full-sha256>
```

## Direct mode

```bash
ogv ingest-object   --source /path/to/object   --vault-root /path/to/vault   --digest sha256:<64-lowercase-hexadecimal-characters>
```

An exact byte count may be supplied with `--expected-size`.

## Behavior

1. The source must be a regular file and not a symlink.
2. Source bytes are hashed while copied to a temporary file inside the target
   object directory.
3. The source metadata must remain stable for the complete read.
4. A digest or size mismatch aborts and removes the temporary file.
5. A verified temporary file is promoted without overwriting an existing path.
6. The stored destination is verified again.
7. The source is never moved or modified.
8. A matching pre-existing object returns `already_present`.
9. A conflicting pre-existing object is never overwritten.

Version v0 uses same-filesystem hard-link promotion to obtain no-overwrite
atomic publication. A vault filesystem that does not support this operation is
rejected rather than handled non-atomically.
