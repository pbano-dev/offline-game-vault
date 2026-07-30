# offline-game-vault

Offline, reproducible orchestration for preserving, verifying, materializing,
and running personally owned Windows games.

The repository contains source code, schemas, tests, documentation, and
sanitized fixtures. Commercial payloads, private saves, private identity,
proprietary redistributables, raw runtime logs, and the binary object store are
outside Git.

## Implemented command families

```text
object verification and content-addressed ingestion
profile verification and deterministic inventory
safe staged materialization and guarded removal
capsule-driven direct-Wine materialization
Bottles managed mutable deployment and execution
generic persistent-state backup, verification, restoration, and rollback
UMU/Proton materialization with exact archived dependencies
capsule operational audit
```

UMU profiles can declare exact runners, Steam Linux Runtime archives, offline
caches, archive policies, symlink manifests, hardlink manifests, protected
files, and a controlled offline environment. Update suppression is not network
isolation: containment remains a separate caller or adapter responsibility.

## Architecture status

Generation `0` uses a content-addressed immutable object store and declarative
capsules. A human-readable catalog and transactional publication workflow have
been validated against a private collection, but are not yet exposed as stable
core command families. They are documented as accepted architectural
directions, not as silently implemented features.

Start with:

- `ARCHITECTURE.md`
- `CHANGELOG.md`
- `docs/validation.md`
- `docs/UMU_BACKEND.md`
- `docs/DSR_UMU_REFERENCE.md`
- `docs/NEXT_STEPS.md`

## Sanitized repository fixtures

Public fixtures contain schemas, documentation, host contracts, and sanitized
acceptance evidence. They contain no game payload, save, private identity,
runner, proprietary binary, or supplemental commercial content.

Current fixtures:

- `fixtures/dark-souls-remastered/`
- `fixtures/sekiro-shadows-die-twice/`

Fixture object granularity follows the canonical archive model: one
self-contained game object per title plus exact shared execution dependencies.
Executables and DLLs already contained in the game object are embedded
artifacts or protected files, not duplicated first-class objects.

Success of one execution profile does not validate another profile, and success
of one game does not transfer functional acceptance to a different game.
