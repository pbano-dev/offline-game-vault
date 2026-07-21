# offline-game-vault

Offline, reproducible vault for preserving, verifying, materializing, and
running personally owned Windows games on Linux and Windows.

Implemented command families:

```text
object verification and ingestion
profile verification and inventory
safe materialization and removal
capsule-driven playable materialization for direct Wine
persistent launchers and state-aware uninstall
Bottles mutable deployment and execution
generic persistent-state backup, verification, and restoration
capsule operational audit
```

Start with:

- `ARCHITECTURE.md`
- `docs/validation.md`
- `docs/cli-state-management.md`
- `docs/cli-playable.md`
- `fixtures/dark-souls-remastered/`

## Sanitized repository fixtures

Repository fixtures contain schemas, documentation, host contracts, and
sanitized acceptance evidence. They contain no game payload, save, private
identity, runner, proprietary binary, or supplemental commercial content.

Current fixtures:

- `fixtures/dark-souls-remastered/`
- `fixtures/sekiro-shadows-die-twice/`

All fixtures share the same core structure. Additional host contracts are
optional and must correspond to profiles declared by that fixture. A missing
untested profile is not filled by copying another game's contract.

Fixture object granularity follows the canonical archive model: one
self-contained game object per title plus exact shared runner or runtime
objects. Executables and DLLs already contained in the game object are recorded
as embedded artifacts or protected files, not duplicated as first-class vault
objects.
