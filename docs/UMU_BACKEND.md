# Modular UMU backend

Offline Game Vault 0.10.0 adds a capsule-driven UMU backend without treating
UMU as direct Wine.

## Separation of responsibilities

- Immutable game objects contain game/media data and may contain a sealed,
  nested prefix baseline.
- Shared UMU objects contain the preserved UMU launcher, Proton build, portable
  Python, and Steam Linux Runtime.
- Operational launchers and sanitizers are capsule assets.
- Required configuration and selectable saves are state archives.
- Published materializations are writable derivatives outside the vault.

The adapter never copies a launcher out of an immutable game object.

## Commands

```text
ogv materialize-umu --capsule CAPSULE --profile PROFILE \
  --vault-root VAULT --state-root STATE --destination DEST [--save ID]

ogv verify-umu --destination DEST
ogv run-umu --destination DEST [-- ARGS...]
ogv remove-umu --destination DEST --confirm-state-preserved
```

`materialize-umu` verifies each immutable dependency through the core verifier,
preflights every archive member and link target, extracts only to private
staging, injects state according to capsule policy, installs hash-pinned
capsule launchers, verifies protected-file and symlink manifests, and
atomically publishes without replacing an existing destination.

`run-umu` verifies before launch, invokes the capsule launcher, invokes the
capsule sanitizer after exit, and verifies again. Network containment remains
an explicit launcher contract and is not inferred merely from the `umu`
adapter label.

## Archive support and boundaries

The backend accepts `tar`, `tar.gz`, and `tar.zst`. Hardlinks are rejected by
default unless the exact layout mapping opts in through
`archive_policy.allow_hardlinks=true`. Special files remain rejected. Absolute
symlinks are rejected unless the exact path and target are declared in
`allowed_absolute_symlinks`.

A nested prefix archive is extracted only into the writable derivative. The
sealed baseline is never executed in place.

## Persistent state

State archives use two policies:

- `always`: injected for every materialization and not exposed as a save choice.
- `selectable`: injected only when its ID is selected.

Protected manifests may use `when_save` so a save manifest is verified only
when that save archive was selected.

## Removal

A materialization containing injected state cannot be removed unless the
caller explicitly confirms that state was preserved. This confirmation does
not itself create a backup.

## Preserved offline dependencies

An UMU profile may declare `offline_environment` to bind a materialization to
preserved local XDG data and cache trees. The core verifies the declared
runtime family/version markers and every hash-pinned cache file, then supplies
`XDG_DATA_HOME`, `XDG_CACHE_HOME`, and `UMU_RUNTIME_UPDATE=0` to both the
launcher and sanitizer.

Archive policy is declared per immutable object mapping. Hardlinks and
absolute symlinks remain rejected by default. A pinned runtime object may opt
in to preserving them, while the preflight still rejects unsafe names,
hardlink targets outside the archive, and members nested below link entries.

Symlink manifests may set `allow_unresolved=true` for a fully enumerated
runtime subtree whose links are meaningful only inside pressure-vessel.
Hardlink manifests verify the exact inode-group topology after materialization
and after gameplay. These options preserve runtime structure; they do not
provide network isolation.

A local dependency contract is not functional acceptance. The profile remains
a candidate until a clean restoration launches with the network isolated,
loads the intended save and content, closes normally, and verifies again.

## Public architecture and acceptance boundary

The public backend contains no game-specific path, AppID, private identity, or
object digest.

A profile may declare exact runtime and cache dependencies, archive policy,
symlink and hardlink manifests, protected files, and offline environment
settings. The adapter verifies those declarations; it does not discover a
similar installed dependency and substitute it.

`UMU_RUNTIME_UPDATE=0` is update suppression, not network isolation. A profile
claiming isolation needs a separately enforced and tested containment method.

The sanitized DSR reference in `docs/DSR_UMU_REFERENCE.md` records the generic
problems and solutions discovered during a private acceptance test. It does not
publish private collection evidence or transfer acceptance to another game.

## Verification evidence

`verify-umu` reports protected-file, symlink, and `hardlink_group_count` values. The topology counts are part of the verification evidence.

## Shared runtime selection for experimental variants

A Proton runner is paired with the Steam Linux Runtime named by the archived
runner's `toolmanifest.vdf` `require_tool_appid`; runtime generation is not
guessed from a directory name or from another game's profile. The current
supported mappings are:

```text
1391110 → steamrt2 → soldier_platform_*
1628350 → steamrt3 → sniper_platform_*
4183110 → steamrt4 → steamrt4_platform_*
```

The shared runtime archive is inspected before selection. Corrupt objects,
missing `VERSIONS.txt`, `_v2-entry-point`, `mtree.txt.gz`,
`pressure-vessel/bin/pv-verify`, `var`, or a unique matching platform
directory are excluded. A missing exact family match aborts before the game is
materialized.

