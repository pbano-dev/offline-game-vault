# Offline Game Vault — Architecture 0.11

Status: **implemented generation-0 core with validated extensions**
Schema generation: **0**
Last updated: **2026-08-03**

## 1. Purpose

Offline Game Vault preserves, verifies, materializes, and runs personally owned
Windows games without requiring future downloads.

The normal flow is:

```text
select capsule
→ verify declared objects
→ resolve exact archived dependencies
→ materialize outside the vault
→ restore declared persistent state
→ launch under an explicit host contract
→ retain verification and acceptance evidence
```

The immutable vault is an archive and source of truth. A materialization is a
derived working installation.

## 2. Non-goals

The project is not:

- a game distributor;
- a replacement for Wine, Bottles, Proton, UMU, Flatpak, or Windows;
- a universal package manager;
- a promise that every future host can execute every preserved profile;
- permission to overwrite originals or silently regenerate baselines;
- evidence that one game's acceptance applies to another game.

## 3. Trust hierarchy

When sources conflict, use this order:

1. real archived object tree;
2. recorded hashes, manifests, and command output;
3. acceptance evidence and receipts;
4. metadata and credits retained with the game;
5. official upstream documentation;
6. project documentation;
7. secondary sources.

Verification compares against an existing baseline. Regeneration defines a new
baseline. They are never the same operation.

## 4. Operational collection layout

The current collection layout is:

```text
<Vault>/
├── 00_CATALOG/
│   ├── GAMES/
│   ├── SHARED/
│   ├── BY_DIGEST/
│   ├── UNREFERENCED/
│   ├── CATALOG.json
│   └── README.md
├── 01_IMMUTABLE_VAULT/
│   ├── objects/
│   │   └── sha256/<2>/<2>/<digest>
│   └── VAULT_INVENTORY.json
├── 02_CAPSULES/
│   └── <capsule-id>/
├── 03_PERSISTENT_STATE/
│   └── <capsule-id>/
├── 04_RECEIPTS/
│   └── <capsule-id>/
├── INDEX.json
├── COLLECTION_LAYOUT.json
└── COLLECTION_SHA256.txt
```

### 4.1 Authority

- `01_IMMUTABLE_VAULT/objects/sha256/` is the physical content-addressed store.
- `02_CAPSULES/` is the declarative source for games and execution profiles.
- `03_PERSISTENT_STATE/` contains private mutable state with its own lifecycle.
- `04_RECEIPTS/` records operations and acceptance.
- `00_CATALOG/` is a human-readable, regenerable view.
- `INDEX.json`, inventory, layout, and collection manifest are control-plane
  metadata.

The catalog never replaces a digest or capsule. Removing a catalog entry does
not remove an object, and an object is never deleted merely because the current
catalog marks it unreferenced.

## 5. Object identity and granularity

Preferred identity is:

```text
sha256:<64 lowercase hexadecimal characters>
```

The physical CAS path depends only on the digest. Human-readable names,
versions, type, and scope are metadata.

A capsule declares one first-class non-shared game object. Files already inside
that archive remain embedded artifacts or protected files. Shared runners,
runtimes, backends, caches, tools, and authorized host dependencies are separate
objects only when independently materialized and reusable.

Object taxonomy is descriptive:

```text
kind:
  game-payload
  runner
  runtime
  backend
  tool
  cache
  host-dependency
  documentation
  state-seed
  historical
  other

scope:
  shared
  game-specific
  historical
```

Changing taxonomy does not move CAS content.

## 5.1 Taxonomy authority and consistency

Generation `0` retains `roles` and `shared` as operational compatibility
fields. Optional `kind` and `scope` are human-facing metadata and must not
contradict them. The validator permits shared and game-specific first-class
dependencies, rejects contradictory scope/shared pairs, requires direct roles
for runner/runtime/documentation kinds, and keeps original-only or
derived-only files in `embedded_artifacts`.

Classification never changes a CAS path.

## 6. Capsules and profiles

A capsule describes the preserved work and may contain several independent
execution recipes, including Bottles, direct Wine, UMU/Proton, native Windows,
or a historical virtual machine.

Profiles are operational recipes and provenance records. They do not contain
maturity, recommendation, or authorization state. Independent acceptance
reports and receipts record what was actually tested for an exact game,
component set, host contract, and date.

The original capsule and a derived operational profile are different records.
Derived work must retain provenance and must not replace the preserved original
silently.

### 6.1 Composition is requested assembly

A user may request a Bottles, Direct-Wine, or UMU composition whenever a
technically compatible source recipe and the required preserved components are
available.

The core synthesizes a private operational profile, leaves the published
capsule unchanged, and selects runners only from verified immutable Vault
objects. No network download or system-runner fallback is permitted.
Acceptance evidence is neither copied into the generated profile nor used as a
permission gate.

A composition may still fail for a material reason: a missing or corrupt
object, unsafe topology, incompatible runner, absent preserved UMU backend,
missing Steam Linux Runtime family, ambiguous source layout, or unsafe
destination.

## 7. Persistent and regenerable state

Persistent state includes:

- saves;
- identity required by those saves;
- explicitly declared user configuration.

It is private, mutable, and separate from immutable game objects.

Regenerable state may include ordinary logs, temporary directories, or caches
only after that behavior has been demonstrated. A cache-like name is not proof
that data can be discarded.

The state engine uses an explicit state root and relative item paths. It never
guesses paths from a title, AppID, username, or adapter. Restore requires a
verified source and a mandatory rollback snapshot.

## 8. Materialization boundary

Materialization:

1. verifies every required object;
2. extracts through the safe archive layer;
3. applies only declared mappings and policies;
4. restores accepted persistent state transactionally;
5. verifies protected files and filesystem topology;
6. publishes the destination atomically where the filesystem permits;
7. emits a receipt.

The immutable object store is not a daily writable installation.

A cross-filesystem operation must stage on the destination filesystem before an
atomic rename. A failed publication must restore the prior control plane.

## 9. Adapter boundary

Host-specific behavior belongs in adapters, not in game IDs or hard-coded
paths.

Implemented families include:

```text
Bottles
direct Wine
UMU/Proton
```

Adapters are responsible for capability checks, exact dependency deployment,
launch planning, environment construction, cleanup, and receipts.

The capsule states requirements. It does not remember a private host layout.

## 10. Direct-Wine backend

The direct-Wine backend composes the generic verified materializer, state
manager, protected-file verifier, and portable runtime.

It uses explicit archive-root mappings, prefix operations, Wine executables,
runtime directories, arguments, environment, and network policy.

Generation `0` does not simulate unsupported network isolation. A claim that
cannot be enforced is rejected.

## 11. Bottles backend

Bottles deployments are mutable derivatives. The preserved source is never
registered or modified in place.

Deployment verifies the copied tree, rewrites only declared identity fields,
retains a receipt, and requires explicit stopped-process and state decisions
before removal.

A Bottles export does not imply that the exact runner is present. Runner
preservation is a separate dependency decision.

## 12. UMU/Proton backend in 0.10.0

UMU profiles can declare:

- exact game and prefix layout;
- exact Proton runner;
- exact Steam Linux Runtime family and version;
- archived UMU and Python components when required;
- offline download caches;
- required runtime and cache files;
- archive policy for absolute symlinks and hardlinks;
- expected symlink topology;
- expected hardlink topology;
- protected files;
- a sanitizer for declared regenerable state;
- an offline environment.

The adapter verifies objects before extraction, verifies layout and topology,
sanitizes before final verification, and promotes only a verified staged tree.

`UMU_RUNTIME_UPDATE=0` suppresses runtime updates. It is not network isolation.
Network containment remains a separate explicit control.

## 13. Symlinks, hardlinks, and volatile runtime state

POSIX topology is part of the preserved contract when the runtime depends on
it.

- Absolute symlinks are rejected unless a layout policy explicitly permits
  them and verification accounts for them.
- Broken or escaping symlinks are rejected unless a narrowly scoped contract
  explicitly permits a known archival form.
- Hardlink groups are verified from a manifest, not inferred from file content.
- A volatile runtime path may be normalized only by a declared sanitizer whose
  behavior is covered by tests.

A regenerated `tmp-*` token may be normalized to the archived canonical token
only when content and topology remain equivalent. The sanitizer must not erase
unknown runtime state.

## 14. External host dependencies

A materialization is not self-contained while a required symlink resolves to a
host-installed file.

The accepted process is:

```text
detect external target
→ classify and authorize dependency
→ capture regular files into an immutable object
→ verify digest, size, architecture, and archive round trip
→ add the object to the profile
→ rewrite targets as internal relative symlinks
→ hide the original host paths during acceptance
```

This process is not a license to copy arbitrary proprietary host content.
Redistribution rights remain separate from personal preservation.

## 15. Human-readable catalog

The CAS is optimized for identity, not file-browser legibility.

`00_CATALOG` provides `.ogvref` JSON references grouped by game, digest, and
primary function. References are small metadata files, not copies, hardlinks,
or alternative authorities.

The catalog generator has been validated against a real collection. Core CLI
integration, schema validation for `.ogvref`, and idempotent `catalog verify`
commands remain pending.

## 16. Transactional publication

Publication updates a control plane, not immutable object bytes.

A complete transaction may update:

- the canonical capsule tree;
- profile-specific persistent-state registration;
- acceptance and operation receipts;
- `INDEX.json`;
- immutable inventory;
- collection layout;
- collection manifest;
- the regenerable catalog.

Publication stages on the destination filesystem, preserves before-images,
validates the result, and rolls back on failure.

This workflow has been validated externally. It is not yet a stable core
command family.

## 17. Reproducibility and containment

Reproducibility and containment are independent:

```text
reproducibility:
  exact objects, versions, manifests, environment, and restoration

containment:
  network namespace, mount namespace, hidden host paths, and process policy
```

A reproducible package may still have network access. A network-isolated launch
may still depend on unarchived host components. Acceptance records both
dimensions separately.

## 18. Acceptance

A profile is not complete merely because its process starts.

Per-game acceptance normally checks:

- clean restoration;
- menu or equivalent initial state;
- preserved save loaded;
- owned DLC loaded where applicable;
- gameplay reached;
- no blocking online requirement;
- normal close;
- post-run protected-file verification;
- post-run symlink and hardlink verification;
- privacy audit;
- state behavior;
- actual network isolation when claimed.

Architecture and automated mechanics are reusable. Functional acceptance is
specific to each game and exact profile.

## 19. Privacy

The public repository must not contain:

- private absolute paths;
- usernames or hostnames;
- host UID/GID or runtime-session paths;
- UUIDs copied from the host;
- raw Wine, Bottles, Proton, DXVK, UMU, or build logs;
- credentials;
- save-bound private identity;
- commercial payloads or proprietary redistributables.

Public evidence must be minimized and sanitized. Required private identity
belongs in private persistent state and a documented exception, never in a
public fixture.

Text, configuration, registry-like data, and symlink targets are audited
separately.

## 20. Repository boundary

Git contains:

- orchestrator source;
- schemas;
- tests;
- documentation and ADRs;
- sanitized fixtures;
- sanitized validation records.

Git does not contain:

- the binary CAS;
- commercial game payloads;
- saves or private identity;
- large prefixes;
- proprietary dependencies without redistribution permission;
- local patch receipts or raw laboratory logs.

## 21. Versioning

Project releases use semantic package versions. Schema generations are explicit
integers beginning at `0`.

A package-version change may add compatible generation-0 fields. A breaking
semantic change requires a new schema generation or a documented migration.

Unsupported future generations are rejected rather than guessed.

## 22. Implementation status

Implemented in the `0.10.1` work tree:

- content-addressed ingestion and verification;
- safe staged materialization;
- deterministic inventory;
- generic persistent-state transactions;
- Bottles deployment;
- direct-Wine playable materialization;
- UMU materialization with offline dependencies;
- archive, symlink, hardlink, protected-file, and environment verification.

Validated architecture pending core command integration:

- catalog build and verification;
- external-dependency capture workflow;
- end-to-end transactional profile publication;
- GUI job orchestration.
