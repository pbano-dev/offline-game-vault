# ADR 0009: Bottles uses mutable derivatives with rebuildable registration

- Status: accepted
- Date: 2026-07-18
- Updated: 2026-08-05

## Context

The immutable materialization is preservation evidence. Bottles is an active
prefix manager and may update configuration, caches, registry files, save data,
and other prefix state during discovery and execution.

Bottles 64.1 resolves its default paths from `XDG_DATA_HOME` and stores managed
bottles under `Paths.bottles`. A configured `custom_bottles_path` can replace
that directory. Bottles resolves a named non-system runner under
`Paths.runners/<runner>`. The CLI launches a bottle by name, not by an
arbitrary prefix path.

A composition destination selected by the operator must remain the
authoritative writable derivative. Publishing the complete prefix and game
inside the private Bottles data tree makes the selected destination
non-authoritative and prevents the generated root operations from being
portable.

A Bottles Flatpak may not be allowed to traverse an arbitrary external
destination by its persistent permissions. A per-invocation
`--filesystem=<MATERIALIZATION_ROOT>` grant provides access without installing
a persistent Flatpak override.

Primary sources and evidence:

- Bottles 64.1 `bottles/backend/globals.py`
- Bottles 64.1 `bottles/backend/managers/manager.py`
- Bottles 64.1 `bottles/backend/utils/manager.py`
- Bottles CLI documentation
- Bottles backup and duplicate documentation
- host acceptance test showing discovery of an externally stored bottle through
  a managed symlink when the invocation receives access to the external root

Source locations:

```text
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/globals.py
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/managers/manager.py
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/utils/manager.py
https://docs.usebottles.com/advanced/cli
https://docs.usebottles.com/bottles/backups
```

## Decision

Offline Game Vault never registers an immutable source object as the writable
Bottles prefix.

The public composition path, `ogv compose --backend bottles`, publishes one
external materialization at the requested `--destination`:

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

The external root is authoritative. Bottles receives only a rebuildable
registration entry:

```text
<BOTTLES_MANAGED_PATH>/<BOTTLE_NAME>
    -> <DESTINATION>/payload/prefix
```

The deployment:

1. validates the capsule, neutral composition contract, source materialization,
   selected runner, and persistent-state baseline;
2. requires a new external destination outside both the immutable collection
   and the managed Bottles directory;
3. copies and verifies the source prefix in sibling staging;
4. restores verified persistent state before publication;
5. separates the game payload from the mutable prefix while retaining the
   expected in-prefix game path through a relative symlink;
6. rewrites the top-level bottle identity for the selected name;
7. writes sanitized receipts and root operation scripts;
8. fsyncs and atomically publishes without replacement;
9. creates the exact managed registration symlink;
10. verifies that Bottles enumerates the registered bottle while granted access
    to the external root.

Every generated Bottles CLI invocation includes:

```text
flatpak run   --filesystem=<MATERIALIZATION_ROOT>   --unshare=network   --command=bottles-cli   com.usebottles.bottles ...
```

The filesystem grant is scoped to that invocation. It is not a persistent
Flatpak override. `--unshare=network` is execution isolation and remains
separate from preservation and reproducibility.

`JUGAR.sh` resolves its own root, verifies the materialization and registration,
recreates a missing registration only when the target name is unused, and then
directly executes the configured game through `bottles-cli run`.

The adapter never downloads, installs, updates, or substitutes a runner or DLL
component. The exact runner must already be present in Bottles. Preservation of
the Bottles application and its Flatpak runtime is a separate package-level
concern and is not claimed by this decision.

## Legacy low-level adapter

The historical low-level commands `deploy-bottles`,
`verify-bottles-deployment`, `plan-bottles-launch`, `run-bottles`, and
`remove-bottles-deployment` retain their managed-directory contract for
backward compatibility.

They are not the publication model used by `compose --backend bottles`.

## Removal

`DESINSTALAR.sh` verifies the external receipt and removes only:

1. the exact managed symlink when it points to this materialization's
   `payload/prefix`; and
2. this external materialization root.

It refuses regular directories, foreign symlinks, unsafe roots, and unconfirmed
removal. It never removes runners, component stores, or other bottles.

Because the prefix is intentionally mutable, removal does not reject state
created during gameplay. State preservation and stopped-process confirmation
remain explicit operator responsibilities.

## Consequences

Advantages:

- the selected destination is the actual materialization root;
- game and prefix are not duplicated in the private Bottles data tree;
- registration can be reconstructed after relocation or clean restoration;
- generated play, verify, and uninstall operations remain at the external root;
- existing bottles and runners are not overwritten or removed;
- no persistent Flatpak filesystem override is required;
- immutable source evidence remains unchanged.

Limitations:

- the implementation supports Linux Bottles Flatpak;
- source hardlinks are rejected rather than silently expanded;
- the active Bottles installation and exact runner remain host dependencies;
- Bottles may mutate the external prefix after publication;
- the deployment receipt records the initial baseline and registration
  relationship, not a claim that the mutable prefix remains byte-identical
  after use.
