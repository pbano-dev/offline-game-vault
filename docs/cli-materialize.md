# Safe materialization

## Materialize one verified profile

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli materialize   --capsule fixtures/<game>/capsule.json   --profile <profile-id>   --vault-root <vault-root>   --destination <new-working-directory>
```

The destination must not exist and must be outside the immutable vault.

The operation:

1. builds the read-only plan;
2. verifies every profile dependency;
3. creates a sibling staging directory;
4. extracts each supported tar object into `objects/<object-id>/`;
5. preserves safe internal runner symlinks and hardlinks;
6. rejects unsafe links, traversal, absolute paths, backslashes, duplicate
   members, special files, and overwrites;
7. verifies every vault object again after use;
8. writes `materialization-receipt.json`;
9. atomically promotes staging without replacing an existing destination.

Supported formats in v0:

```text
tar
tar.gz
file
```

Other formats fail explicitly.

## Resulting layout

```text
<DESTINATION>/
  materialization-receipt.json
  objects/
    <object-id>/
      <exact extracted archive tree>
```

The orchestrator does not assume the internal top-level directory of a bottle
or runner archive. The Bottles adapter must inspect the materialized tree.

## Receipt privacy

The receipt stores `destination: "."` and object-relative paths. It does not
store the vault root or original source paths. It includes a UTC creation time
because it is evidence of a concrete materialization, not a deterministic
vault inventory.

## Remove a materialization

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli   remove-materialization   --destination <working-directory>
```

Removal requires a valid receipt and refuses unknown top-level paths.

When the receipt declares persistent state, removal refuses until that state
has actually been backed up. Only then use:

```bash
PYTHONPATH=src python -B -m offline_game_vault.cli   remove-materialization   --destination <working-directory>   --confirm-state-preserved
```

`--confirm-state-preserved` is an assertion, not a backup operation. Automated
state preservation is part of the later state-management milestone.
