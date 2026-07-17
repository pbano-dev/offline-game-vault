# `ogv verify-object`

`ogv verify-object` verifies one immutable regular-file object by streaming
SHA-256. It never regenerates a digest.

## Capsule mode

```bash
ogv verify-object   --capsule fixtures/dark-souls-remastered/capsule.json   --object-id dsr-bottle-baseline   --vault-root /path/to/immutable-vault   --json
```

## Direct mode

```bash
ogv verify-object   --path /path/to/object   --digest sha256:<64-lowercase-hexadecimal-characters>
```

An optional exact byte count can be supplied with `--expected-size`.

## Exit status

```text
0  digest and optional size match
1  readable object does not match
2  invalid request, missing file, unsafe path, symlink, or verification error
```

## Safety properties

- reads in bounded chunks;
- rejects a symlink object path;
- rejects symlink components below a capsule's vault root;
- rejects directories and special files;
- checks file metadata before and after hashing;
- never modifies the object;
- never writes or updates a manifest;
- capsule paths must remain inside the declared vault root.

Version v0 verifies regular-file objects only. A canonical digest model for
directory objects has not yet been defined.
