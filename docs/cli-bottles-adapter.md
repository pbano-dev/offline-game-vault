# Bottles adapter CLI

The Bottles backend has two publication contracts:

1. `ogv compose --backend bottles` creates an external, operator-selected
   materialization and a rebuildable registration symlink in Bottles.
2. The historical low-level `deploy-bottles` command creates a mutable
   derivative directly below the managed Bottles directory.

The external composition path is the normal user-facing workflow. Both paths
leave the immutable Vault and source materializations unchanged and perform no
downloads.

## External composition

### Preconditions

Verify on the target host:

- Bottles Flatpak is installed;
- the exact preserved runner required by the selected composition is available
  to install or already installed through the core's runner deployment logic;
- the selected destination does not exist and its parent is a regular writable
  directory;
- the destination is outside the immutable collection and outside the managed
  Bottles directory;
- the selected bottle name does not already exist;
- any required persistent-state backup is verified.

### Compose

```bash
ogv compose   --collection <COLLECTION_ROOT>   --capsule <CAPSULE_ID_OR_PATH>   --backend bottles   --runner <RUNNER_ID>   --destination <NEW_EXTERNAL_DESTINATION>   --bottle-name <NEW_NON_COLLIDING_NAME>   [--state-backup <VERIFIED_BACKUP>]   --json
```

The bottle name must match:

```text
[A-Za-z0-9][A-Za-z0-9._-]{0,127}
```

The result's `destination` is exactly the requested external destination.

The published layout contains:

```text
<DESTINATION>/
├── JUGAR.sh
├── VERIFICAR.sh
├── DESINSTALAR.sh
├── metadata/
└── payload/
    ├── game/
    └── prefix/
```

Bottles receives a symlink registration only:

```text
<BOTTLES_MANAGED_PATH>/<BOTTLE_NAME>
    -> <DESTINATION>/payload/prefix
```

No game or prefix copy is stored below the managed Bottles directory.

### Play

Run from any current directory:

```bash
<DESTINATION>/JUGAR.sh [GAME_ARGUMENTS...]
```

The script:

- resolves its own root;
- checks the Bottles Flatpak;
- verifies the deployment receipt, bottle configuration, runner, game
  executable, and registration;
- safely recreates a missing registration when the name is unused;
- grants Bottles access only to the materialization root for that invocation;
- executes the configured game directly through `bottles-cli run`.

The effective command is:

```text
flatpak run   --filesystem=<DESTINATION>   --unshare=network   --command=bottles-cli   com.usebottles.bottles   run -b <BOTTLE_NAME> -e <GAME_EXECUTABLE> ...
```

The filesystem grant is not persisted. Network isolation remains a separately
validated execution property.

### Verify

```bash
<DESTINATION>/VERIFICAR.sh
```

Verification covers:

- the external root and deployment receipt;
- required metadata and operation scripts;
- `payload/game` and `payload/prefix`;
- the configured game executable;
- bottle identity and selected runner;
- the registration-to-prefix relationship;
- Bottles enumeration with access to the external root.

It does not claim that mutable gameplay state remains equal to the initial
baseline.

### Remove

First back up required persistent state and stop Bottles and all Wine processes
using the materialization.

Then run:

```bash
<DESTINATION>/DESINSTALAR.sh   --confirm-state-preserved   --confirm-stopped
```

The operation removes only the exact registration symlink and the external
materialization. It rejects regular managed directories, foreign symlinks, and
unsafe roots. It never removes runners or other bottles.

## Legacy managed-directory adapter

The following low-level commands retain their existing contract for backward
compatibility.

### Deploy

```bash
ogv deploy-bottles   --capsule <CAPSULE_JSON>   --profile linux-bottles-flatpak   --materialization <MATERIALIZATION>   --bottles-path <BOTTLES_MANAGED_PATH>   --name <NEW_NON_COLLIDING_NAME>
```

This stages, verifies, and publishes a mutable bottle directly below the
managed Bottles directory. The resulting bottle contains:

```text
.ogv-bottles-deployment.json
```

### Verify, plan, run, and remove

```bash
ogv verify-bottles-deployment   --bottles-path <BOTTLES_MANAGED_PATH>   --name <DEPLOYED_NAME>

ogv plan-bottles-launch   --bottles-path <BOTTLES_MANAGED_PATH>   --name <DEPLOYED_NAME>

ogv run-bottles   --bottles-path <BOTTLES_MANAGED_PATH>   --name <DEPLOYED_NAME>

ogv remove-bottles-deployment   --bottles-path <BOTTLES_MANAGED_PATH>   --name <DEPLOYED_NAME>   --confirm-state-preserved   --confirm-stopped
```

These commands do not implement the external composition layout and should not
be used to infer the destination returned by `compose --backend bottles`.
