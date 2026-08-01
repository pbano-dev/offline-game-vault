# Experimental materialization

Offline Game Vault can synthesize a private operational variant without
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
ogv materialize-experimental \
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
ogv materialize-experimental \
  --collection-root <VAULT> \
  --capsule <VAULT>/02_CAPSULES/<CAPSULE_ID>/capsule.json \
  --backend bottles \
  --runner <RUNNER_ID> \
  --bottles-path <BOTTLES_MANAGED_PATH> \
  --bottle-name <NEW_BOTTLE_NAME>
```

The selected runner is installed from its immutable Vault archive. An existing
local runner with the same name is rejected unless it was installed from the
same preserved object and its complete tree still verifies.

`--play` launches the resulting managed bottle after deployment.

## 4. UMU/Proton

List preserved backend templates:

```bash
ogv list-umu-templates \
  --collection-root <VAULT> \
  --json
```

Then materialize:

```bash
ogv materialize-experimental \
  --collection-root <VAULT> \
  --capsule <VAULT>/02_CAPSULES/<CAPSULE_ID>/capsule.json \
  --backend umu \
  --runner <PROTON_RUNNER_ID> \
  --umu-backend <CAPSULE_ID>/<PROFILE_ID> \
  --destination <NEW_DESTINATION>
```

The backend template supplies preserved UMU, portable Python, and Steam Linux
Runtime components. The selected Proton is mapped separately from its preserved
runner object.

## 5. Status and receipts

The source profile may be `verified`, `candidate`, `experimental`,
`not_tested`, or `unavailable`. Status does not authorize the operation. When
the exact backend profile is absent, the core selects a compatible neutral
Linux source and synthesizes the requested backend profile. Use
`--source-profile` only to override that deterministic selection or resolve an
equally suitable ambiguity.

Every synthesized variant records:

```text
kind: experimental
acceptance_inherited: false
source_profile_id
backend
runner_id
backend_template  # UMU only
```

Materialization success means the declared pieces assembled and verified. It
does not prove gameplay, saves, DLC, media, controller support, isolation, or
normal shutdown.
