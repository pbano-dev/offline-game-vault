# Dark Souls Remastered fixture

This is a **sanitized metadata fixture** for Offline Game Vault.

It contains no commercial payload, private save, private identity, runner, or proprietary binary.

## Verified source facts

- Steam AppID: `570940`
- Preserved version: application `1.03.1`, regulation `1.04`
- Canonical executable:
  `drive_c/Games/DARK_SOULS_REMASTERED/DarkSoulsRemastered.exe`
- Verified runner declaration: `ge-proton11-1`
- The normalized Bottles archive imports and runs without `.fvs2`.
- Existing save loading, controller input, normal exit, and network-isolated execution were tested.

## Important corrections found during fixture creation

The historical package README is not the final authority where it conflicts with the archive tree.

1. The actual archived save is under `Documents/NBGI/DARK SOULS REMASTERED/...`,
   not under `AppData/Roaming/Dark Souls Remastered/...`.
2. `GSE Saves/settings/configs.user.ini` is separate identity/configuration state.
3. The public fixture redacts the private account identifier.
4. The old size section is obsolete after removing `.fvs2`.
5. Direct Wine and native Windows instructions remain unverified.
6. The imported bottle resolved `ge-proton11-1` because it was already installed on
   the test host; automatic offline runner deployment remains untested.
7. The archive did not visibly contain `dosdevices/`; therefore, the statement that
   it can always be used as a standard Wine prefix without Bottles is not yet proven.

## Fixture status

The Bottles/Flatpak profile is verified with limitations.

The following remain untested:

- clean host without Bottles or runner;
- offline bootstrap of Flatpak/Bottles;
- automatic runner deployment;
- direct Wine recovery;
- native Windows recovery;
- portable USB export.

## Fixture correction after repository review

- Each unverified execution profile now references its own host contract.
- `cache/dxvk_shader` is no longer classified as regenerable because deletion has not been tested.
- The redacted save path uses `ACCOUNT_ID_REDACTED`, avoiding angle-bracket placeholders.
- The verified Bottles contract does not require colon-bearing filenames.


## Generic state-management migration

The two persistent-state entries now explicitly declare `required: true`.

This public fixture remains sanitized and is therefore intentionally
non-operational for backup or restore:

```bash
ogv audit-capsule \
  --capsule fixtures/dark-souls-remastered/capsule.json
```

The audit is expected to report a valid sanitized fixture with warnings and
`operational: no`.

Actual capture and restore require the private capsule with the exact save
identifier:

```bash
ogv preserve-state \
  --capsule <PRIVATE_CAPSULE> \
  --state-root <DERIVED_BOTTLE_ROOT> \
  --backup <NEW_PRIVATE_BACKUP> \
  --confirm-stopped
```

No DSR path is embedded in the state-management implementation.
