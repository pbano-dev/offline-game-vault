# Composition materialization

Offline Game Vault can synthesize a private operational composition without
rewriting the source capsule or claiming functional acceptance.

Supported backends in this command family:

```text
bottles
direct-wine
umu
```

Windows-native materialization is intentionally outside this phase.

## 1. List preserved runners

```bash
ogv list-preserved-runners \
  --collection-root <VAULT> \
  --json
```

The command lists only objects that:

- are indexed as `shared-runner`;
- exist at their canonical immutable path;
- match the recorded size and SHA-256;
- expose an unambiguous Wine/Wineserver pair;
- expose `proton` when UMU compatibility is reported.

No network lookup, download, or system-runner fallback occurs.

## 2. Direct-Wine

```bash
ogv compose \
  --collection-root <VAULT> \
  --capsule <VAULT>/02_CAPSULES/<CAPSULE_ID>/capsule.json \
  --backend direct-wine \
  --runner <RUNNER_ID> \
  --destination <NEW_DESTINATION>
```

Add `--play` to launch after successful materialization. Additional game
arguments go after `--`.

A selected state backup may be supplied with `--state-backup`.

## 3. Bottles

```bash
ogv compose \
  --collection-root <VAULT> \
  --capsule <VAULT>/02_CAPSULES/<CAPSULE_ID>/capsule.json \
  --backend bottles \
  --runner <RUNNER_ID> \
  --bottle-name <NEW_BOTTLE_NAME>
```

The core discovers the effective managed directory through
`bottles-cli info bottles-path`. The optional `--bottles-path` argument is only
an assertion for automation; a different or arbitrary directory is rejected.

The selected runner is installed from its immutable Vault archive. An existing
local runner with the same name is rejected unless it was installed from the
same preserved object and its complete tree still verifies. Heavy staging is
created inside the managed Bottles directory and the final bottle must be
enumerated by `bottles-cli` before success is reported.

`--play` launches the resulting managed bottle after deployment.

## 4. UMU/Proton

```bash
ogv compose \
  --collection-root <VAULT> \
  --capsule <VAULT>/02_CAPSULES/<CAPSULE_ID>/capsule.json \
  --backend umu \
  --runner <PROTON_RUNNER_ID> \
  --destination <NEW_DESTINATION>
```

The core reads the preserved Proton runner's `toolmanifest.vdf` and resolves
the exact Steam Linux Runtime required by `require_tool_appid`. A qualifying runtime must be registered globally as `shared-umu-runtime`,
exist in the immutable inventory and CAS, match its recorded size and SHA-256,
match the required family, and pass a complete archive inspection before it is
selectable. Incomplete runtime objects are ignored rather than
being discovered after a game has been copied.

The user does not select another game's capsule or profile as a backend.
Diagnostic inventory is available with:

```bash
ogv list-shared-umu-runtimes \
  --collection-root <VAULT> \
  --json
```

The diagnostic JSON uses a `component_sets` array. Each item records the
independently verified backend component, Steam Linux Runtime component,
deterministic UMU entrypoint, runtime family, and content-derived
`component_set_id`.

The selected Proton remains a separate preserved runner object.

The archived Steam Runtime `var` subtree is retained in the writable
derivative. Generated sanitization is deliberately narrow: it removes only
known regenerable files and never treats the whole subtree as disposable.

## Destination-local staging

All large working trees are created on the filesystem selected for the final
derivative:

- Bottles: below the selected managed Bottles directory;
- Direct-Wine and UMU: below the selected destination parent.

Hidden `.ogv-*` working directories are verified and then published
atomically. They are removed after success or failure. `/tmp` is not used for
large game copies.

## 5. Recipes, evidence, and provenance

Source profiles are recipes and provenance records. They do not carry maturity
or authorization state. The core selects a technically compatible neutral Linux
source and synthesizes the requested backend profile. Use `--source-profile`
only to override that deterministic selection or resolve an equally suitable
ambiguity.

The generated profile records the selected backend, runner, dependencies, and
source-derived layout. UMU additionally writes a host contract containing the
independently resolved backend component, Steam Linux Runtime component,
component-set identifier, and runner identifier.

Acceptance reports remain independent evidence. They are not copied into a
generated profile and are not used to permit or prohibit materialization.
Materialization success means the declared pieces assembled and verified. It
does not prove gameplay, saves, DLC, media, controller support, isolation, or
normal shutdown.

## Canonical operational interface

Every published Linux materialization exposes the same root interface:

```text
JUGAR.sh
VERIFICAR.sh
DESINSTALAR.sh
```

The implementation behind those scripts is backend-specific and copied into
the derivative. Receipts remain descriptive evidence; the scripts are the
operational entry points used by the GUI.

UMU compositions additionally require a complete preserved `steamrtN` tree with
`VERSIONS.txt`, `_v2-entry-point`, `pressure-vessel`, and exactly one matching
`steamrtN_platform_*` directory. Launch is isolated from the network and
runtime downloads are never used as repair.

