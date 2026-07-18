# Bottles adapter CLI

The Bottles adapter converts a verified materialization into a mutable bottle
managed by the active Bottles installation.

It never writes into the immutable vault or the materialized source bottle.

## Preconditions

Verify on the target host:

- Bottles Flatpak is installed;
- the effective managed bottles path was obtained from
  `bottles-cli info bottles-path`;
- the exact runner and DLL components required by `bottle.yml` are already
  installed;
- the destination bottle name does not exist;
- the materialization receipt is present and valid.

The adapter performs no downloads.

## Deploy

```bash
ogv deploy-bottles \
  --capsule <CAPSULE_JSON> \
  --profile linux-bottles-flatpak \
  --materialization <MATERIALIZATION> \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --name <NEW_NON_COLLIDING_NAME>
```

The name must match:

```text
[A-Za-z0-9][A-Za-z0-9._-]{0,127}
```

Deployment is staged, verified, fsynced, and atomically published without
replacement.

The resulting mutable bottle contains:

```text
.ogv-bottles-deployment.json
```

The receipt is sanitized and stores no absolute source or host path.

## Verify

```bash
ogv verify-bottles-deployment \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --name <DEPLOYED_NAME>
```

This verifies:

- the deployment receipt;
- the top-level `Name`, `Path`, `Custom_Path`, and `Runner`;
- the configured entrypoint.

It does not claim that gameplay state remains equal to the deployment
baseline.

## Plan launch

```bash
ogv plan-bottles-launch \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --name <DEPLOYED_NAME>
```

The displayed command is sanitized and uses `<BOTTLES_PATH>` instead of the
private absolute path.

## Run

```bash
ogv run-bottles \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --name <DEPLOYED_NAME>
```

For `network: isolated`, the adapter invokes Bottles Flatpak with
`--unshare=network`.

This proves isolation only when validated on the real host. It does not prove
that the game itself never attempts networking.

## Remove

First back up all state marked `preserve_on_remove`, and stop Bottles and all
Wine processes using the deployment.

Then run:

```bash
ogv remove-bottles-deployment \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --name <DEPLOYED_NAME> \
  --confirm-state-preserved \
  --confirm-stopped
```

The command refuses unrecognized directories and never removes a deployment
without the stopped confirmation. State confirmation is required whenever the
receipt declares persistent state that must be preserved.
