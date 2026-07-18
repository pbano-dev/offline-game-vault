# ADR 0009: Bottles uses managed mutable derivatives

- Status: accepted
- Date: 2026-07-18

## Context

The immutable materialization is preservation evidence. Bottles is an active
prefix manager and may update configuration, caches, registry files, save data,
and other prefix state during discovery and execution.

Bottles 64.1 resolves its default paths from `XDG_DATA_HOME` and stores managed
bottles under `Paths.bottles`. A configured `custom_bottles_path` can replace
that directory. Bottles resolves a named non-system runner under
`Paths.runners/<runner>`. The CLI launches a bottle by name, not by an
arbitrary prefix path.

Primary sources:

- Bottles 64.1 `bottles/backend/globals.py`
- Bottles 64.1 `bottles/backend/managers/manager.py`
- Bottles 64.1 `bottles/backend/utils/manager.py`
- Bottles CLI documentation
- Bottles backup and duplicate documentation

Source locations:

```text
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/globals.py
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/managers/manager.py
https://github.com/bottlesdevs/Bottles/blob/64.1/bottles/backend/utils/manager.py
https://docs.usebottles.com/advanced/cli
https://docs.usebottles.com/bottles/backups
```

## Decision

Offline Game Vault never registers the immutable materialized bottle itself as
the writable Bottles prefix.

`ogv deploy-bottles` creates a mutable derivative under the effective managed
bottles directory. The operator must provide a new, non-colliding bottle name.

The deployment:

1. validates the capsule, Bottles profile, materialization receipt, and
   `prefix_baseline` object;
2. locates exactly one `bottle.yml`;
3. validates the configured entrypoint;
4. rejects special files, broken symlinks, escaping symlinks, and source
   hardlinks;
5. hashes the source tree;
6. copies into sibling staging;
7. verifies the staged copy against the source;
8. rewrites only the top-level `Name`, `Path`, and `Custom_Path` fields in the
   staged `bottle.yml`;
9. writes a sanitized deployment receipt;
10. fsyncs and atomically publishes with no replacement.

The original materialization is not edited.

The adapter never downloads, installs, updates, or substitutes a runner or DLL
component. Host component readiness remains an explicit preflight.

For profiles whose capsule declares `network: isolated`, `ogv run-bottles`
launches Bottles Flatpak with:

```text
flatpak run --unshare=network --command=bottles-cli ...
```

This is execution isolation. It is separate from preservation and
reproducibility.

## Removal

`ogv remove-bottles-deployment` removes only a bottle containing a valid OGV
deployment receipt whose name matches its managed directory.

Removal requires explicit confirmation that:

- Bottles and all processes using the deployment are stopped;
- persistent state marked `preserve_on_remove` has been backed up.

Because the deployed bottle is intentionally mutable, removal does not reject
new files created during gameplay. The receipt and confirmations are the
guardrails.

## Consequences

Advantages:

- immutable preservation evidence remains unchanged;
- existing bottles are never overwritten;
- the active Bottles installation can discover the derivative normally;
- launch intent and network policy are recorded;
- removal is recognizable and guarded.

Limitations:

- the initial implementation supports Linux only;
- source hardlinks are rejected rather than silently expanded;
- exact host component identity must be verified separately;
- Bottles may mutate the derivative after publication;
- the deployment receipt records the pre-launch baseline, not a claim that the
  mutable bottle remains byte-identical after use.
