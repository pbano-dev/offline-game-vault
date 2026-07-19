# Capsule-driven playable materialization

Version `0.9.0` provides a direct-Wine backend built on the existing verified
object materializer and persistent-state engine.

## Materialize and optionally play

```bash
ogv materialize-playable \
  --capsule <PRIVATE_CAPSULE>/capsule.json \
  --profile <DIRECT_WINE_PROFILE> \
  --vault-root <VAULT_ROOT> \
  --destination <DESTINATION> \
  --state-backup <ACCEPTED_STATE_BACKUP> \
  --play
```

The state backup is mandatory when the capsule declares state with
`backup=true`.

A valid existing destination with the same capsule, profile, and playable
contract is verified and reused. An unrecognized or inconsistent destination
is never repaired silently.

## Verify

```bash
ogv verify-playable --destination <DESTINATION>
```

This checks the anchored receipt, required paths, declared prefix operations,
and all protected files.

## Play

```bash
ogv run-playable --destination <DESTINATION>
```

Additional game arguments follow `--`:

```bash
ogv run-playable --destination <DESTINATION> -- <GAME_ARGUMENTS>
```

The materialization also contains its persistent launcher, whose name is
declared by the profile, for example:

```bash
<DESTINATION>/jugar_sekiro.sh
```

The launcher invokes only the archived runner and writes
`receipts/last-play.json`.

## Remove

Unchanged state:

```bash
ogv remove-playable --destination <DESTINATION>
```

Changed state aborts by default. Preserve it first:

```bash
ogv remove-playable \
  --destination <DESTINATION> \
  --export-state <NEW_OR_EMPTY_DIRECTORY>
```

Explicit destructive removal:

```bash
ogv remove-playable \
  --destination <DESTINATION> \
  --discard-state
```

The generated uninstaller exposes the same policy:

```bash
<DESTINATION>/desinstalar_sekiro.sh \
  --export-state <NEW_OR_EMPTY_DIRECTORY>
```

## Playable profile contract

A direct-Wine profile may contain:

```json
{
  "playable": {
    "schema": 0,
    "backend": "wine",
    "paths": {
      "prefix": "prefix",
      "runner": "runner/<RUNNER>",
      "wine": "runner/<RUNNER>/files/bin/wine",
      "wineserver": "runner/<RUNNER>/files/bin/wineserver",
      "runtime": "runtime",
      "launcher": "jugar.sh",
      "uninstaller": "desinstalar.sh"
    },
    "layout": [
      {
        "object": "<PREFIX_OBJECT>",
        "source": "<ROOT_INSIDE_ARCHIVE>",
        "destination": "prefix"
      },
      {
        "object": "<RUNNER_OBJECT>",
        "source": "<ROOT_INSIDE_ARCHIVE>",
        "destination": "runner/<RUNNER>"
      }
    ],
    "prefix_operations": [
      {
        "type": "mkdir",
        "path": "prefix/dosdevices"
      },
      {
        "type": "symlink",
        "path": "prefix/dosdevices/c:",
        "target": "../drive_c"
      }
    ],
    "protected_files": [
      {
        "path": "prefix/<RELATIVE_FILE>",
        "digest": "sha256:<DIGEST>",
        "size": 0
      }
    ]
  }
}
```

Every dependency must be mapped exactly once. Archive roots and protected
identities are never inferred.

## Current limitations

- Linux direct Wine only;
- Python 3 required by the copied portable runtime;
- no network-isolation backend yet;
- no silent fallback to Bottles or system Wine;
- no Windows-native launcher;
- no claim that window-ready startup latency is measured;
- no cross-host acceptance implied by a single verified profile.
