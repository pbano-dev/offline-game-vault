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
user-requested component compositions from preserved Vault pieces
capsule operational audit
```

UMU profiles can declare exact runners, Steam Linux Runtime archives, offline
caches, archive policies, symlink manifests, hardlink manifests, protected
files, and a controlled offline environment. For published UMU derivatives,
the generated `JUGAR.sh` validates the preserved runtime and launches it inside
a private network namespace with updates disabled.

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
- `docs/COMPONENT_COMPOSITIONS.md`
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

## Component compositions

Acceptance evidence is descriptive and does not authorize or prohibit a
composition. A user may request a Bottles, Direct-Wine, or UMU composition with
any technically compatible runner preserved in the Vault. The core synthesizes
an operational profile and leaves the source capsule unchanged.

The composition path never downloads components and never falls back to a
system runner. Missing or corrupt objects, unsafe paths, incompatible runners,
and absent preserved backend pieces remain blocking errors.

### Operational scripts in materializations

Bottles, Direct-Wine, and UMU derivatives all publish `JUGAR.sh`,
`VERIFICAR.sh`, and `DESINSTALAR.sh` at their root. The GUI uses these scripts
after materialization instead of reconstructing backend launch commands.
UMU refuses incomplete preserved runtimes and launches with network isolation;
it never downloads a missing Steam Linux Runtime.
