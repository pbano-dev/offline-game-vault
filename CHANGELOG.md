# Changelog

All notable project changes are documented here.

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

### Validated outside the stable CLI

- A clean UMU restoration with exact Steam Linux Runtime topology.
- Internalization of required Vulkan layer files previously resolved from the
  host.
- Offline functional acceptance with host dependency paths hidden.
- Transactional publication and regeneration of a human-readable catalog.

These validated workflows are architectural evidence. Catalog and publication
remain pending as stable core command families.
