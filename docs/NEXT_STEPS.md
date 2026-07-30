# Next steps after the UMU 0.10.0 integration

This document is public project context. It deliberately excludes private
collection paths, object digests, save identities, operation IDs, and raw logs.

## Completed in 0.10.0

- The UMU adapter, schema, CLI, tests, documentation, and ADRs were committed
  on `main`.
- GitHub Actions validated commit `6eba2839c10ee8fd1b905f2f6379bf6ffac6dad1`.
- Tagging and a hosted release remain separate decisions.

## Priority 2: move validated external workflows into the core

Implement generic commands for:

- catalog build and verification;
- normalized comparison of declared volatile runtime paths;
- hardlink counts in the public verification result;
- detection of symlinks that escape the materialization;
- authorized capture of external host dependencies;
- relative-target rewriting and post-rewrite verification;
- transactional publication of capsule, state registration, receipts, index,
  inventory, layout, collection manifest, and catalog;
- rollback from before-images;
- explicit privacy audit reports.

No command may contain title-specific paths, AppIDs, object hashes, or runner
names.

## Priority 3: catalog schema and lifecycle

- Add a JSON Schema for `.ogvref`.
- Make catalog generation idempotent.
- Add `catalog verify`.
- Define whether previous catalogs are retained or replaced.
- Keep the CAS path based only on digest.
- Audit unreferenced objects through capsules and receipts before deletion.

## Priority 4: profile identifiers

A verified profile may retain a historical ID containing `candidate`. Decide
between immutable IDs with authoritative status or a migration with aliases.
Never rename an ID directly after it is referenced by state and receipts.

## Priority 5: GUI integration

The GUI should call core operations and display:

- source capsule versus derived profile;
- immutable objects versus mutable state;
- required and shared dependencies;
- external dependencies detected and internalized;
- clean-restoration status;
- functional acceptance;
- privacy result;
- reproducibility versus network containment;
- publication and catalog status.

The GUI must not embed game-specific paths, hashes, runners, or dependency
lists.

## Per-game workflow after DSR

For each new title:

1. inventory the exact edition, store, version, DLC, and bonus material;
2. determine executable, Steamworks, protection, anticheat, and state;
3. choose Bottles or UMU from demonstrated requirements;
4. generate the contract;
5. restore cleanly;
6. perform one isolated functional acceptance;
7. verify state, protected files, privacy, symlinks, and hardlinks;
8. publish transactionally.

The architecture is reusable. Functional acceptance is not inherited.
