# Changelog

## 0.17.1 — 2026-08-12

### Fixed

- The state_root auto-derivation added in 0.17.0 pointed at
  ``03_PERSISTENT_STATE/<capsule_id>/`` and expected preserved
  ``umu.state_archives`` tarballs to live directly at that path. The
  real convention places them two segments deeper —
  ``03_PERSISTENT_STATE/<capsule_id>/<profile_id>/active/`` — so the
  materializer could not find them and failed with
  ``State archive <id> is absent or not a regular file.`` immediately
  after fase 6 routed the composition correctly. This release fixes
  the auto-derivation path.

- The nested ``<profile_id>/`` segment accommodates capsules that ship
  more than one UMU profile with their own preserved tarballs; the
  ``active/`` leaf separates the operative tarballs from the parallel
  ``related-artifacts/`` subtree, which carries preserved evidence
  intentionally kept out of the materialisation.

### Changed

- ``_derive_state_root_for_umu_native`` gains a required ``profile_id``
  argument (an internal helper; the CLI surface is unchanged). Its
  unit tests are updated for the new signature and directory shape.

## 0.17.0 — 2026-08-11

### Added

- ``compose`` gains two new arguments applicable to ``--backend umu``:
  ``--state-root PATH`` and ``--save-id ID``. They control how the
  preserved ``umu.state_archives`` of a umu-native capsule are placed
  during materialization: ``--state-root`` names the directory holding
  the archive tarballs, and ``--save-id`` picks a selectable save
  policy in addition to any ``always`` archives.

- When ``--state-root`` is omitted, ``compose_umu`` derives it from the
  documented convention:
  ``<collection_root>/03_PERSISTENT_STATE/<capsule_id>/``. When that
  directory exists it is used automatically; when it does not, the
  argument is left unset and the materializer's own error surfaces the
  concrete argument the user must supply. Zero configuration for the
  canonical layout, explicit override for anything else.

### Fixed

- Capsules with a preserved UMU rich contract that also declare
  ``state_archives`` with an ``always`` policy — such as Devil May
  Cry 5, whose ``dmc5-required-configuration-v1.tar.zst`` provisions
  a required baseline configuration — are no longer blocked by the
  materializer's ``--state-root is required by the UMU contract``
  error immediately after fase 6 succeeded in routing them through
  compose. Fase 6 opened the door; 0.17.0 hands the visitor the key.

### Changed

- ``compose_umu`` rejects ``--state-root`` and ``--save-id`` explicitly
  when the selected source profile is not umu-native, because
  synthesized UMU contracts have no ``state_archives`` to place and
  silently ignoring the arguments would misrepresent user intent.
- The CLI layer rejects the same arguments upfront when
  ``--backend`` is not ``umu``, before any composition work begins.

## 0.16.0 — 2026-08-10

### Added

- ``compose --backend umu`` now recognises capsules that carry a
  preserved UMU rich contract (``adapter: umu`` with a ``umu`` block
  of ``schema: 0``) as a first-class source kind called ``umu-native``.
  The capsule's ``umu`` block is a complete recipe — preserved
  launchers with digests, protected manifests, state archives with
  policies, symlink manifests, nested archives, mutable paths and
  layout — and does not need to be combined with global runtime,
  backend and runner references at compose time. Capsules such as
  Devil May Cry 5, which have shipped this shape from the start, can
  now be materialised through the compose pipeline (and, via the CLI,
  from the GUI) instead of only through the low-level ``materialize-umu``.
- ``_SOURCE_PRIORITIES["umu"]`` prefers ``umu-native`` over the
  synthesized neutrals when a capsule offers one: an author-preserved
  contract wins over a runtime overlay.
- ``CompositionResult.backend_result`` now carries an explicit
  ``source_kind`` field ("umu-native" or the synthesized alternative)
  so downstream consumers can tell the two apart.

### Changed

- ``compose_umu`` resolves the source kind before touching shared
  runtime resolution. For ``umu-native`` sources the capsule is the
  operational capsule; neither ``_select_shared_umu_runtime`` nor
  ``_umu_overlay`` is invoked, and ``require_state_backup`` is off
  (state comes from the preserved ``umu.state_archives``, not from a
  user-provided backup).

  The previous order — shared runtime resolution before source kind —
  caused ``compose_umu`` to fail with "no matching global UMU
  component composition" for umu-native capsules whose preserved
  runner needed a Steam Linux Runtime family not otherwise present in
  the Vault (a very real DMC5 + Proton 9.0-203 + steamrt3 case).

- For synthesized ``umu`` sources the flow is unchanged: shared
  runtime + overlay + ``require_state_backup=True``, exactly as
  before.

### Fixed

- Devil May Cry 5, and any future capsule that ships a preserved
  rich UMU contract, is no longer blocked from ``compose`` by
  ``"Linux source profile cannot provide the material required by
  umu."``.

## 0.15.0 — 2026-08-10

### Changed

- ``metadata/generated-files.json`` now classifies files by content, not
  by top-level path prefix. When a materialization publishes, the
  per-object manifests that just travelled to
  ``metadata/manifests/`` are loaded into a
  ``Counter[(size, sha256_hex)]`` catalogue; every regular file at the
  destination whose ``(size, hex)`` appears in the catalogue is treated
  as preserved object content and skipped, and the rest — composition
  launchers, receipts, runtime helpers, and any file a user added by
  hand — becomes an entry in the manifest.

  The old model derived "object-owned prefixes" from each backend's
  primary receipt (``paths.prefix``, ``paths.runner``, ``paths.runtime``
  or ``layout.prefix``, ``layout.game``). It worked for Direct-Wine and
  Bottles, where files land under a predictable subtree, but silently
  degenerated on UMU: the materializer rebases and mixes object content
  into a synthetic tree, and ``paths.prefix`` covered only a fraction
  of it. A Sims 2 UMU compose ended up with 27691 entries in
  ``generated-files.json`` — most of them object content misclassified
  as generated — and zero object files matched during verification.

- ``write_generated_files_manifest`` takes ``object_manifest_paths``
  instead of ``object_owned_prefixes``. Callers pass the paths returned
  by ``copy_manifests_to_materialization``; the function silently
  filters sidecars out of the catalogue build.

- The three ``_object_prefixes_from_*`` helpers in ``composition.py``
  were removed. The three ``compose_*`` no longer inspect the primary
  receipt for prefix hints; the copied manifests carry the information
  the classifier needs.

### Fixed

- Fase 4's evidence is honest across backends. ``generated-files.json``
  contains a handful of entries for the launchers, receipts, and
  runtime helpers the composition actually authored, on Direct-Wine,
  Bottles, and UMU alike.

## 0.14.0 — 2026-08-09

### Added

- Materializations are now self-verifiable end-to-end. Every
  ``compose_*`` packages the per-object manifests generated at Vault
  ingest into the destination alongside a manifest of the files it
  wrote on the fly, and the backend runtime that ships with every
  materialization now consumes that evidence.
- ``metadata/manifests/sha256/<aa>/<bb>/<hex>`` byte-copies of every
  participating object manifest (plus their ``.sha256`` sidecars) so a
  detached materialization carries its own integrity evidence.
- ``metadata/generated-files.json`` records every composition-authored
  file (launchers, receipts, runtime helpers) with its SHA-256 and
  size, so bit rot on any of them is detectable offline.
- ``<primary-receipt>.sha256`` sidecar for each backend's main receipt.
- ``metadata/ogv_manifest_check.py`` shared standalone helper copied
  into every materialization alongside the backend runtime; loaded via
  ``importlib`` so no sys.path manipulation is required.
- ``VERIFICAR.sh`` performs real verification against the travelled
  evidence: recomputes the receipt sidecar, rehashes every entry in
  ``generated-files.json``, verifies each per-object manifest against
  its sidecar, and counts destination files that match any manifest
  entry.

### Changed

- ``ingest-object``, ``compose``, and the runtime ``verify`` share a
  strict format for the per-object manifest (see
  ``object_manifest.format_manifest``): ``key:value`` headers, blank
  line, ``<hex> <size> <path>`` entries.
- The runtime ``verify`` gains a ``check_manifests`` argument. Internal
  callers (``play``, ``uninstall``) leave it off so game launches
  remain cheap; the CLI ``verify`` subcommand — the one
  ``VERIFICAR.sh`` invokes — enables it.
- Manifest verification is deliberately "based on existence, not
  enumeration": corruption of composition-controlled evidence is a
  hard failure, but files the user added by hand or object content a
  materialization legitimately discarded show up only as informational
  counts. Extras never block a verification.

### Fixed

- Pre-flight in ``compose_*`` refuses to materialize when any object
  lacks a manifest in the Vault, with a message pointing at
  ``ogv generate-missing-manifests``. This prevents silent production
  of materializations that could not be self-verifiable.

## 0.13.0 — 2026-08-08

### Added

- Per-object content manifests: for every preserved object, a text manifest
  lists each contained file with its SHA-256 digest, so a materialization
  can be verified without consulting the Vault it was extracted from.
  Layout: ``01_IMMUTABLE_VAULT/manifests/sha256/<aa>/<bb>/<hex>`` with a
  ``<hex>.sha256`` sidecar.
- ``generate-object-manifest`` computes the manifest for one preserved
  object in capsule or direct mode, verifying the object bytes before
  writing anything.
- ``generate-missing-manifests`` walks the Vault and ensures every object
  has a valid manifest, deriving the archive format from capsule
  declarations or from ``INDEX.json`` labels for shared components
  (runners, backends, UMU runtimes).
- ``ingest-object`` automatically generates the per-object manifest on
  successful ingest. Capsule mode reads the format from the capsule;
  direct mode accepts a new ``--format`` argument. ``--no-manifest``
  skips the manifest step for bulk ingestion or deferred generation.

### Changed

- ``--format`` in ``ingest-object`` capsule mode must agree with the
  capsule's declared format; a mismatch aborts the ingest with an
  explicit conflict error before anything reaches the Vault.
- Manifest failures during ingest are surfaced as warnings and JSON
  fields without invalidating the ingest itself: the object is in the
  Vault, its manifest can be generated later with
  ``generate-object-manifest`` or ``generate-missing-manifests``.

## 0.12.2 — 2026-08-05
### Fixed

- Historical state backups remain verifiable when their declarations form a
  safe subset of the current capsule definition.
- Compatibility requires unchanged state IDs, paths, kinds, and sensitivity.
- Newly added optional state and relaxed required flags no longer invalidate
  an otherwise intact historical backup.
- Removed state, changed paths, and newly hardened requirements remain
  blocking.
- Restoration applies only declarations represented by the selected receipt;
  newer optional state is left untouched.
- Backend compositions use the verified historical receipt item count instead
  of requiring it to equal the current capsule declaration count.
- Bottles composition now requires and publishes to the requested external
  destination instead of copying the derivative into the private Bottles tree.
- The managed Bottles directory stores only a guarded, rebuildable symlink to
  the external `payload/prefix`; game and prefix data are not duplicated.
- Generated Bottles play, verify, and uninstall operations live at the
  external root and grant Flatpak access per invocation without persistent
  overrides.
- Bottles composition results report the selected external destination, and
  removal refuses foreign registrations, other bottles, and runners.
- Bottles accepts historical capsules whose only Linux source is a
  playable Wine profile, deriving the neutral fields from the recorded
  layout instead of requiring a neutral host contract.
- Archived full-prefix objects whose game already sits inside the prefix
  are materialized without copying the payload a second time.
- A game nested inside the prefix at a location other than
  game_destination_in_prefix remains blocking.
- Bottles runner deployment resolves before any payload is extracted, so
  a runner that cannot be used fails in seconds instead of after the
  whole materialization.
- A hand-installed Bottles runner that is byte-identical to the preserved
  object is adopted rather than rejected, and one that differs no longer
  blocks the composition: the verified copy is installed beside it under
  an OGV-namespaced name and the generated bottle.yml names it.
- A Vault-installed runner whose tree no longer matches its marker is
  still refused; integrity failures are reported, not routed around.

## 0.12.1 — 2026-08-05

### Fixed

- Composition keeps the derived capsule responsible for backend
  materialization while using the original operational capsule for
  persistent-state verification and restoration.
- Bottles, Direct-Wine, and UMU no longer audit an intentionally incomplete
  temporary overlay when validating `--state-backup`.
- The state capsule must carry the same `capsule_id` as the derived capsule.
- Regression tests cover lower-level state routing and all three public
  composition call sites.

## 0.12.0 — 2026-08-04

### Added

- Backend-neutral `compose --state-backup` support for Bottles, Direct-Wine,
  and UMU/Proton.
- Explicit UMU prefix-root propagation from the neutral playable contract.
- Shared restoration evidence recording the selected backup identity,
  pre-restore snapshot identity, backend state root, copied baseline receipt,
  and baseline receipt SHA-256.
- Verification of restoration evidence during Bottles and UMU verification.

### Changed

- The composition boundary now requires a verified state backup whenever the
  capsule declares preservable persistent state, regardless of backend.
- Bottles restores into the staged bottle root; UMU restores into the declared
  staged prefix; Direct-Wine retains its verified prefix restoration path.
- UMU reuse rejects a different persistent-state baseline.
- Generic persistent state participates in UMU guarded removal.
- Legacy `umu.state_archives` remains a separate low-level mechanism and cannot
  be combined with generic `--state-backup`.
- Project version advanced from `0.11.4` to `0.12.0`.

### Security

- State restoration occurs before atomic publication and uses the generic
  verified backup engine with a mandatory pre-restore snapshot.
- Evidence paths must remain relative, regular, and free of symlink traversal.
- Bottles and UMU reject altered baseline-state receipts during verification.

### Validation

- Synthetic tests restore a verified backup physically in Bottles and UMU.
- Tests cover source preservation, receipt evidence, matching-baseline reuse,
  rejection of a different UMU baseline, and altered-evidence detection.
- The complete repository suite passes with the backend-neutral contract.

## 0.11.4 — 2026-08-03
### Fixed

- UMU discovery selects one deterministic preserved backend entrypoint and
  records it in the resolved component set.
- Generated UMU launchers execute that exact path instead of searching the
  materialized tree again.
- Diagnostic JSON and composition results expose backend, Steam Runtime, and
  `component_set_id` provenance without obsolete runtime or acceptance fields.
- Regression tests execute `JUGAR_UMU.sh` and both diagnostic output modes.
- Archived Steam Runtime `var` content is retained in writable derivatives;
  generated sanitizers remove only explicitly identified regenerable paths.
- Package metadata, runtime receipts, and `ogv --version` now agree on 0.11.4.
- ADR 0016 records the rationale for state-free component composition,
  independent acceptance evidence, deterministic UMU binding, and narrow
  sanitization.

- Host-side symlink verification now treats only concrete pressure-vessel `runtime_var/tmp-*` roots as container-context paths; broken links elsewhere remain blocking.
## 0.11.3 — 2026-08-01

### Fixed

- Bottles composition materialization again uses the directory reported by
  `bottles-cli info bottles-path` for every capsule. An optional
  `--bottles-path` value is only an assertion and cannot redirect deployment
  to an arbitrary directory.
- Bottles heavy staging is created inside that managed directory, the final
  bottle is published atomically there, and the core verifies that
  `bottles-cli` enumerates it before reporting success.
- UMU runtime selection now reads Proton's preserved `toolmanifest.vdf` and
  resolves the exact Steam Linux Runtime family required by
  `require_tool_appid`.
- Runtime validation uses the real platform-directory prefix for each family:
  `soldier_platform_*` for steamrt2, `sniper_platform_*` for steamrt3, and
  `steamrt4_platform_*` for steamrt4.
- Incomplete or corrupt shared runtime objects are excluded before selection.
  A runner is never paired with a different runtime family merely because it
  is the first preserved candidate.

### Validation

- Regression tests cover managed Bottles-path discovery, rejection of an
  arbitrary Bottles path, registration of the published bottle, destination-
  local staging, steamrt3/sniper validation, and exact runner/runtime matching.

## 0.11.2 — 2026-08-01

### Fixed

- Every newly published Bottles, Direct-Wine, and UMU materialization now
  contains executable root-level `JUGAR.sh`, `VERIFICAR.sh`, and
  `DESINSTALAR.sh` scripts plus its authoritative backend receipt.
- Bottles operational scripts are self-contained and no longer depend on a
  source checkout of the core for play, verification, or removal.
- UMU materialization now rejects an absent or incomplete preserved
  `steamrtN` runtime before publication or launch.
- UMU execution is forced through a private network namespace and exports
  `UMU_RUNTIME_UPDATE=0`; missing runtime components therefore abort instead
  of being downloaded.
- Direct-Wine keeps legacy contract launcher names as compatibility aliases
  while always publishing the three canonical root scripts.

### Validation

- Regression tests execute the generated Direct-Wine and Bottles scripts.
- UMU tests use a complete preserved runtime and a synthetic network-isolated
  `systemd-run` harness.
- All materializers assert the canonical operational script set.

## 0.11.1 — 2026-08-01

### Fixed

- Bottles composition materialization now creates its heavy working tree
  inside the user-selected managed Bottles directory, not in the host `/tmp`.
- Direct-Wine and UMU control overlays are also created beside the selected
  destination; backend materializers continue to stage and publish atomically
  on that filesystem.
- UMU no longer accepts a game capsule/profile as a user-selected backend
  template. The core resolves a reusable, content-addressed shared runtime
  automatically.

### Changed

- Reusable UMU/Python/Steam Runtime objects must be explicitly marked
  `shared: true` and carry the `runtime` role.
- UMU receipts record `shared_runtime_id`; source capsule/profile identifiers
  remain provenance and are not runtime identities.
- Schemas continue to accept the deprecated 0.11.0
  `backend_template` provenance field so existing receipts remain valid.
- Added `list-shared-umu-runtimes` for diagnostics. The normal materialization
  command does not require or accept a runtime-template selection.

### Validation

- Tests cover destination-local Bottles staging, cleanup, automatic shared UMU
  runtime selection, and rejection of game-specific runtime objects.

All notable project changes are documented here.

## 0.11.0 — 2026-08-01

### Added

- User-requested component composition for Bottles, Direct-Wine, and
  UMU/Proton.
- Discovery and SHA-256 verification of preserved shared runners.
- Synthesized operational profiles that leave source capsules unchanged,
  including backend profiles absent from the published capsule when another
  compatible neutral Linux source exists.
- Global discovery of preserved UMU/Python backend components.
- Optional materialize-and-play execution in the composition command family.
- Composition provenance for the selected source, backend, runner, and
  resolved runtime where applicable.
- Vault-derived Bottles runner installation with full-tree reuse validation.

### Changed

- Profiles are recipes and no longer contain maturity or
  acceptance-reference fields.
- Source selection is based on technical compatibility; acceptance evidence
  remains independent and descriptive.
- Bottles runner selection no longer falls back to an unverified local
  installation.
- Project version advanced from `0.10.2` to `0.11.0`.

### Security

- Component compositions use only immutable objects already preserved in the
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
- Tests cover synthesis from a neutral source that does not already declare
  the requested backend, runner-object tampering, preserved Bottles runner
  reuse, UMU component composition, and CLI parsing.

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
