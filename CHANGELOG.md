# Changelog

All notable project changes are documented here.

## 0.11.0 — 2026-08-01

### Added

- User-requested experimental materialization for Bottles, Direct-Wine, and
  UMU/Proton.
- Discovery and SHA-256 verification of preserved shared runners.
- Synthesized operational profiles that leave source capsules unchanged,
  including backend profiles absent from the published capsule when another
  compatible neutral Linux source exists.
- Preserved UMU backend-template discovery.
- Optional materialize-and-play execution in the experimental command family.
- Experimental-variant provenance in backend receipts.
- Vault-derived Bottles runner installation with full-tree reuse validation.

### Changed

- Profile status and acceptance evidence are descriptive rather than
  authorization gates.
- `unavailable`, `candidate`, and `not_tested` profiles may be used as source
  evidence for a user-requested experimental variant.
- Bottles runner selection no longer falls back to an unverified local
  installation.
- Project version advanced from `0.10.2` to `0.11.0`.

### Security

- Experimental variants use only immutable objects already preserved in the
  Vault.
- Runner objects are checked against their recorded size and SHA-256 before
  selection.
- Existing Bottles runner directories are rejected unless they were installed
  from the same Vault object and their complete tree still verifies.
- Archive, path, symlink, destination, and atomic-publication protections
  remain mandatory.

### Validation

- Synthetic end-to-end materialization was exercised for Direct-Wine, Bottles,
  and UMU.
- Tests cover an `unavailable` source profile, runner-object tampering,
  preserved Bottles runner reuse, UMU backend composition, and CLI parsing.

## 0.10.2 — 2026-07-31

### Fixed

- Rewrote test manifest construction to remain valid under Python 3.11.
- Decoupled privacy-scanner unit tests from the external `git` executable.
- Added a matrix-version syntax gate before the unit-test step; it compiles
  every Python source in memory and does not create bytecode inside the
  repository.

### Validation

- The complete suite was executed in the local Python 3.11 container used to
  reproduce the failed CI job.
- Repository validation, public privacy scanning, and filesystem hygiene passed.
- The no-write CI sequence was reproduced under Python 3.11, 3.12, 3.13,
  and 3.14 before preparing the corrective commit.

## 0.10.1 — 2026-07-30

### Added

- Repository-wide privacy scanning for tracked text and symlink targets.
- Taxonomy consistency tests and hardlink verification evidence.
- CI coverage for Python 3.11 through 3.14.

### Changed

- First-class dependency validation accepts declared shared or game-specific
  runners, runtimes, backends, tools, caches, documentation, state seeds, and
  authorized host dependencies.
- Optional `kind` and `scope` must not contradict `roles` and `shared`.
- GitHub Actions use immutable commit references.
- `UmuVerificationResult` exposes `hardlink_group_count`.

### Security

- Public validation detects private Unix, removable-media, runtime-UID, and
  Windows user paths, plus absolute symlink targets.

## 0.10.0 — 2026-07-30

### Added

- Capsule-driven UMU/Proton materialization.
- Exact archived UMU, Proton, Steam Linux Runtime, and offline-cache
  dependencies.
- Per-layout archive policy for absolute symlinks and hardlinks.
- Symlink and hardlink manifest verification.
- Offline environment declarations with runtime-update suppression.
- UMU CLI coverage and synthetic offline-dependency tests.
- Architecture decisions for a human-readable catalog over the CAS and for
  self-contained UMU host dependencies.

### Changed

- Capsule schema extended for UMU profiles and object classification metadata.
- Project version advanced from `0.9.0` to `0.10.0`.
- Architecture updated to the operational collection layout.
- Public documentation now separates reproducibility, containment, acceptance,
  immutable objects, persistent state, and derived materializations.
- Duplicate ADR numbering corrected by moving canonical object granularity to
  ADR 0012.

### Fixed

- Added the documented optional `object.kind` and `object.scope` fields to the
  capsule schema.
- Clarified that hardlinks are rejected by default and may be enabled only by
  an explicit archive policy.
- Updated the release checklist after the 0.10.0 commit and CI completion.

### Validated outside the stable CLI

- A clean UMU restoration with exact Steam Linux Runtime topology.
- Internalization of required Vulkan layer files previously resolved from the
  host.
- Offline functional acceptance with host dependency paths hidden.
- Transactional publication and regeneration of a human-readable catalog.

These validated workflows are architectural evidence. Catalog and publication
remain pending as stable core command families.
