# Offline Game Vault — Architecture v0

Status: **Draft**
Schema generation: **0**
Last updated: **2026-07-17**

## 1. Purpose

Offline Game Vault preserves, verifies, materializes, and runs personally owned Windows games without requiring future network access.

The normal user experience is:

```text
select game
→ verify
→ resolve archived dependencies
→ materialize outside the vault
→ prepare mutable state
→ launch
```

The vault is an archive and source of truth. It is not the working installation.

## 2. Non-goals

The project is not:

- a game distributor;
- a replacement for Wine, Bottles, Proton, UMU, Flatpak, or Windows;
- a universal package manager;
- a promise that every future host can run every preserved profile;
- a reason to overwrite original files or regenerate integrity baselines silently.

## 3. Trust hierarchy

When sources conflict, use this order:

1. real archived object tree;
2. cryptographic hashes and manifests;
3. recorded command output and acceptance evidence;
4. metadata shown by the game;
5. official upstream documentation;
6. project documentation;
7. secondary sources.

## 4. Core boundaries

### 4.1 Vault

Immutable, backed up, and optionally mounted read-only.

Contains:

```text
vault/
├── catalog/
├── games/
├── objects/
├── engines/
├── tools/
├── schemas/
├── bootstrap/
└── manifests/
```

### 4.2 Local store

A host-local cache of verified immutable objects copied from the vault.

It may be deleted and rebuilt.

### 4.3 Materialized profile

A host-local runnable installation assembled from archived objects.

It contains engine configuration, game payload, prefix or compatibility state, and launch metadata.

### 4.4 Persistent state

Mutable user data that must survive removal and rematerialization:

- saves;
- required identity;
- user configuration explicitly marked persistent.

### 4.5 Regenerable state

Mutable data that may be deleted:

- logs;
- ordinary shader caches;
- temporary files;
- disposable working prefixes when a baseline exists.

A file that merely looks like a cache is not regenerable until proven so. Media Converter/Foz data is one known counterexample.

## 5. Primary operations

The future orchestrator exposes, conceptually:

```text
verify
plan
materialize
play
remove
repair
export-portable
import-saves
export-saves
```

Every mutating operation must support a dry-run or equivalent plan.

## 6. Dependency resolution

A game profile references exact objects by digest.

Preferred identity:

```text
sha256:<64 lowercase hexadecimal characters>
```

Human-readable names and versions are metadata. They never replace the digest.

The resolver must not silently:

- download missing objects;
- select a newer runner;
- use a system Wine because it has a similar name;
- regenerate manifests;
- replace a failed verified profile with an experimental one.

## 7. Execution profiles

A capsule may declare multiple profiles, for example:

- Bottles on Linux;
- direct Wine on Linux;
- UMU/Proton on Linux;
- native Windows;
- historical VM.

Each profile has its own status:

```text
verified
candidate
experimental
not_tested
unavailable
```

Profiles are independent. Success of one profile does not validate another.

## 8. Adapters

Host-specific details belong in adapters, not in game manifests.

Initial adapter families:

```text
bottles
wine
umu
windows
```

Adapters are responsible for:

- capability detection;
- dynamic path discovery;
- dependency deployment;
- profile preparation;
- process launch;
- isolation options;
- cleanup and receipts.

A manifest must not contain a private absolute host path.

## 9. Host contract

A profile declares requirements, not a remembered machine description.

Examples:

- CPU architecture;
- POSIX filesystem semantics;
- Flatpak availability or archived bootstrap route;
- Vulkan capability;
- user namespaces;
- network namespace support;
- display and audio backends.

The host checker reports:

```text
compatible
warning
incompatible
unknown
```

Unknown is not compatible.

## 10. Materialization rules

1. The vault is never modified.
2. Archived objects are verified before use.
3. Immutable objects are copied, reflinked, or linked only according to a recorded strategy.
4. The golden baseline is never used as the daily writable profile.
5. Persistent saves are separated logically, even when an older source package stored them inside a bottle.
6. Every installation produces a receipt.
7. Removal follows the receipt and preserves saves by default.

## 11. Portable export

A portable export is a self-contained subset of the vault for one or more selected games.

It contains:

- game-specific objects;
- all required shared dependencies;
- schemas and manifests;
- bootstrap launchers;
- documentation;
- integrity metadata;
- a portable profile declaration.

Default behavior on the destination host:

```text
verify USB
→ detect host
→ materialize to host-local storage
→ launch
```

Direct execution from removable media is an optional profile, not the default.

A bootable USB image is a future profile and is not required by architecture v0.

## 12. Integrity and provenance

SHA-256 proves identity against a recorded baseline. It does not prove legitimacy, safety, or future executability.

The project distinguishes:

```text
verify     = compare against an existing baseline
regenerate = define a new baseline
```

Regeneration must never occur automatically during verification or repair.

## 13. Privacy

Never archive or commit by default:

- private absolute paths;
- username or hostname;
- UID/GID tied to the host;
- raw Wine, Bottles, Proton, DXVK, or build logs;
- credentials;
- unrelated host mappings.

Required identity data, such as a save-bound SteamID, must be explicitly declared as a documented exception.

Archives should normalize internal ownership metadata where safe.

## 14. Documentation retained per game

Every preserved game retains:

```text
00_README.md
FICHA_DEL_JUEGO.md
CREDITOS.md
PRESERVADO_POR.md
```

Automation is the normal interface. Documentation remains the recovery path, evidence record, and human-readable explanation.

## 15. Repository boundary

The Git repository contains:

- orchestrator source;
- adapters;
- schemas;
- tests;
- templates;
- documentation;
- sanitized fixtures;
- the project log.

It does not contain:

- commercial game payloads;
- private saves;
- private identifiers;
- large prefixes;
- proprietary redistributables without redistribution permission;
- the canonical binary object store.

## 16. Versioning

Schemas use explicit integer generations beginning at `0`.

Breaking semantic changes require a new generation or a documented migration.

The orchestrator must reject unsupported future schema generations instead of guessing.
