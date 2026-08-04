# Repository validation

Run locally from the repository root:

```bash
python -m pip install -r requirements-ci.txt
python tools/validate_repository.py
```

The validator checks:

- all JSON Schemas, including vault inventory and persistent-state receipts, are valid Draft 2020-12 schemas;
- each fixture `capsule.json` validates;
- host contracts, independent acceptance reports, and receipts validate;
- referenced documents and metadata files exist;
- profile dependencies reference existing object IDs;
- profile and object IDs are unique;
- host-contract platforms match their profiles;
- every `acceptance*.json` report is schema-validated, privacy-audited, and
  required to name the fixture capsule;
- orphan host contracts are rejected, while acceptance reports do not need
  a profile-side reference;
- common absolute private-path patterns are rejected in fixture JSON.

This validation does not verify archived payload hashes because commercial
payloads are outside Git.

The unit suite also exercises safe tar extraction, staged materialization, receipt validation, and guarded removal.

## Bottles adapter validation

The unit suite also verifies:

- a materialized bottle is copied rather than modified in place;
- existing managed bottle names are never overwritten;
- source and staged trees match before identity rewriting;
- unsafe or broken symlinks and special files are rejected;
- `Name`, `Path`, and `Custom_Path` are rewritten only in the derivative;
- deployment receipts validate against the Bottles deployment schema;
- launch plans do not disclose the private managed path;
- isolated profiles include `flatpak run --unshare=network`;
- removal requires stopped and persistent-state confirmations.


## Persistent-state validation

The unit suite also verifies:

- operational and sanitized capsule audit outcomes;
- safe non-overlapping state paths;
- mandatory stopped-process confirmation;
- private `0700`/`0600` backup permissions;
- regular-file and directory capture;
- required and optional missing state;
- SHA-256 and tree-manifest verification;
- payload-tampering detection;
- rejection of symlinks and multiple hard links;
- mandatory pre-restore snapshots;
- restored live-state verification;
- rollback after an injected multi-item restore failure;
- backup and restore receipts against their Draft 2020-12 schemas;
- sanitized CLI and receipt output without absolute host paths.

## Playable direct-Wine validation

The `0.9.0` suite additionally verifies:

- capsule-schema validation of playable layout contracts;
- exact mapping of every profile dependency;
- rejection of overlapping or traversing layout paths;
- reuse of the generic verified materializer and safe tar extractor;
- filesystem capability probes before publication;
- transactional accepted-state restoration;
- regeneration of only declared prefix infrastructure;
- runner-symlink containment;
- protected executable and DLL identities;
- automatic protection of archived Wine and Wineserver executables;
- deterministic generated launchers that pass `bash -n`;
- second execution without rematerialization;
- play receipts and explicit non-instrumentation of window-ready latency;
- default uninstall refusal after state changes;
- verified state export and explicit discard policy;
- removal of only registered top-level paths;
- rollback when protected-file verification fails;
- rejection of unsupported network-isolation claims;
- no regression in Bottles, generic materialization, or state operations.

The public Sekiro fixture records the accepted direct-Wine profile without
including the game, runner, save, private identity, or raw logs.

## Canonical object-granularity validation

Repository validation also enforces:

- exactly one non-shared object with the `game_payload` role per fixture;
- every other first-class object is a shared runner or runtime archive;
- embedded originals and derived binaries are recorded under
  `embedded_artifacts`, not under `objects`;
- every embedded artifact names the canonical game object that contains it;
- profile dependencies include the canonical game object;
- embedded artifact IDs do not collide with first-class object IDs.

## UMU/Proton validation in 0.10.0

The UMU suite additionally verifies:

- schema validation of UMU layout and dependency contracts;
- exact object-to-layout mapping;
- archive policy for absolute symlinks and hardlinks;
- required Steam Linux Runtime files;
- offline cache files by path, digest, and size;
- symlink manifests and unresolved-target policy;
- hardlink-group manifests;
- protected files;
- controlled XDG data and cache roots;
- `UMU_RUNTIME_UPDATE=0`;
- sanitizer execution before final topology verification;
- atomic promotion of a verified staged materialization;
- CLI result and failure behavior;
- no game-specific constants in the adapter.

The test suite uses synthetic objects. Commercial game data, private state, and
proprietary runtime files are not present in Git.

## Component-composition validation in 0.11.0–0.11.4

The composition suite additionally verifies:

- neutral source recipes can produce Bottles, Direct-Wine, and UMU
  compositions when the exact backend recipe is absent;
- preserved runner discovery checks canonical path, size, and SHA-256;
- Proton is classified for Bottles, Direct-Wine, and UMU;
- Bottles installs runners only from immutable Vault archives;
- a previously installed Bottles runner is reused only while its complete tree
  still verifies;
- a global UMU/Python backend, a separate Proton runner, and the exact globally
  registered Steam Linux Runtime can be combined;
- archived runtime `var` content survives materialization, generated
  sanitization, and verification;
- project metadata, package runtime, and the leading changelog release expose
  the same version;
- missing, corrupt, unsafe, or incompatible components remain blocking errors;
- no system runner or network download participates in the synthetic tests.

Independent acceptance reports remain schema-validated evidence. They are not
profile fields and are not permission gates.
