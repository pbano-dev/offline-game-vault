# Sekiro: Shadows Die Twice fixture

This is a **sanitized metadata fixture** for Offline Game Vault.

It contains no commercial game payload, private save, private identity,
runner, proprietary binary, or artbook.

## Verified source facts

- Steam AppID: `814380`
- Preserved version: `1.06`
- Canonical prepared executable:
  `drive_c/Games/Sekiro/sekiro.exe.unpacked.exe`
- Original executable preserved:
  `sekiro_ORIGINAL.exe`
- SteamStub was removed from the derived executable using Steamless.
- Steamworks is locally reimplemented with the preserved gbe_fork build.
- The exact runner declaration is `ge-proton11-1`.
- Clean restoration, restored-save loading, gameplay, normal game exit,
  and Bottles self-exit were verified.
- The accepted state contains the save and gbe identity.
- The artbook is preserved in the private collection but excluded from
  this public fixture.

## Preserved component identities

```text
Sekiro Full Archive:
62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662

Runner:
37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc

Active gbe_fork steam_api64.dll:
cf61e505e63852b24aefb9d9d0712bc1ae45921c7c3a0c01abb1dc0c95c8ca01

Original Steamworks steam_api64.dll:
fc20547408a7c34f0bd4946a34c21aab48a75e3b98dce9e55969f486d37b212f

Original executable:
637aca527538c0ec6e1f136c8ed66046e95dfbdbb1f51926e134d9916398b856

Derived executable:
189b2fed665473c565d983a01c5af87f80d15e5446a74262801077fb1a6fd17c
```

## Important distinctions

- Steamless removes SteamStub; it does not replace Steamworks.
- gbe_fork reimplements Steamworks; it does not remove SteamStub.
- Installed depots do not by themselves prove account ownership.
- The original object and the derived playable capsule are separate.
- Persistent state is preserved outside the immutable Full Archive object.

## Fixture status

The Bottles profile is verified with limitations.

The following remain untested:

- independent external-network denial from inside the sandbox;
- direct Wine recovery;
- native Windows recovery;
- portable USB export;
- execution on a different host or user account.

The exact run-bottles child return code was not persisted during the
clean-restoration recovery. Normal game exit and Bottles self-exit were
observed and recorded separately.

## Collection integration

The canonical collection stores only this three-file fixture core under
`public-fixture/`.

A repository-ready fixture is assembled with:

```text
README.md
acceptance.json
capsule.json
docs/
host-contract.linux-bottles.json
```

using the canonical sanitized documents and Bottles host contract.
