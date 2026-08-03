# Next steps after the composition-composition 0.11.3 integration

This document is public project context. It deliberately excludes private
collection paths, object digests, save identities, operation IDs, and raw logs.

## Completed in 0.10.0

- The UMU adapter, schema, CLI, tests, documentation, and ADRs were committed
  on `main`.
- GitHub Actions validated commit `6eba2839c10ee8fd1b905f2f6379bf6ffac6dad1`.
- Tagging and a hosted release remain separate decisions.

## Completed in 0.10.1

- Aligned taxonomy and first-class dependency validation.
- Added consistency checks for `kind`/`scope` and `roles`/`shared`.
- Added repository-wide public privacy validation.
- Added Python 3.11-3.14 CI coverage with immutable action references.
- Exposed hardlink-group counts in UMU verification results.

Base commit: `41cb284d2a2ff92d2dca21241cb06f4a6614c520`.

## Completed in 0.10.2

- Restored Python 3.11 compatibility in the UMU test module.
- Made privacy-scanner unit tests independent of a Git executable.
- Added an explicit non-writing syntax gate to every Python CI matrix job.

Base commit: `10c918641aa9425499c13a13ba4329b2dca53866`.

## Completed in 0.11.0

- Added user-requested composition compositions for Bottles, Direct-Wine, and
  UMU/Proton.
- Made acceptance and status descriptive rather than authorization gates.
- Restricted runner selection to SHA-256-verified immutable Vault objects.
- Added preserved Bottles runner installation and reuse verification.
- Added automatic composition from shared UMU runtime objects.


## Completed in 0.11.1–0.11.3

- Moved large staging trees beside their final destination.
- Restored Bottles managed-path discovery through `bottles-cli` and made an
  arbitrary deployment directory invalid.
- Added canonical operational scripts to every Linux materialization.
- Enforced network-isolated UMU launch with preserved dependencies only.
- Resolved the exact Steam Linux Runtime from each Proton runner's archived
  `toolmanifest.vdf`, including the steamrt3/sniper naming distinction.
- Excluded incomplete and mismatched shared runtimes before materialization.

## Priority 2: move validated external workflows into the core

Implement generic commands for:

- catalog build and verification;
- normalized comparison of declared volatile runtime paths;
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
lists. It should obtain preserved runners from the core, while shared UMU
runtimes are resolved internally by the core, then call
`compose` for user-requested combinations.
Recommendation and acceptance must remain visible without becoming a hard
permission gate.

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
