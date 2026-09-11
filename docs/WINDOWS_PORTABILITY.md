# Experimental native Windows launchers

This implementation prepares a game on Linux, using the existing composition
flow, and adds native Windows launchers to the resulting writable directory.
It does not reimport, rewrite or repack the Vault's immutable objects, and it
adds no OS selection to composition. The game files are shared by both launchers.
Native Windows composition itself is not implemented.

Status is always `prepared-unverified` or `blocked`; this feature does not
certify any commercial game as playable on Windows. The GUI displays that
status and the reason for blocked preparation. Older Core versions have no
Windows preparation information.

## Files and normal use

| File | Purpose |
| --- | --- |
| `JUGAR.sh`, `JUGAR_LINUX.sh` | Existing Linux backend and selected runner |
| `JUGAR_WINDOWS.bat` | Windows PowerShell 5.1 runtime, native game executable |
| `VERIFICAR_WINDOWS.bat` | Recompute integrity of immutable game files |
| `RECUPERAR_WINDOWS.bat` | Recover an interrupted Windows session on its original host and user |
| `metadata/windows/launch.json` | Derived executable, paths, state mappings, arguments and environment |
| `metadata/windows/files.json` | Game file hashes, reusing composition's hashing pass |

The Windows runtime requires Windows PowerShell 5.1 with full language support
and .NET Framework, and an x86 or x64 game executable. The supported target for
this first implementation is 64-bit Windows with a writable local NTFS
materialization and local user folders. Native Windows CI targets x64 Windows
Server 2022 and 2025. Consumer Windows 10/11 gameplay, ARM, network shares,
cloud-synchronized state folders and other filesystems are not accepted targets.

Copy the complete materialized tree using a method that retains the Linux
files and symlinks if the same copy will return to Linux. Windows does not
execute the Wine/Proton binaries or use their Linux prefix links. The selected
native game and state trees must contain regular files/directories. Storage
must support the game's individual file sizes; copying onto FAT32 can truncate
this workflow before the launcher can verify it.

There is no Windows runner selection: the `.exe` runs directly on the Windows
host. Linux continues to use the selected Bottles, Wine or UMU runner. No Python
or installed `ogv` command is needed by the Windows launcher. Starting through
the GUI dispatches Play/Verify to the host's launcher; the full GUI and
composition toolchain continue to target Linux in this release.

## State lifecycle

Only `persistent_state` declarations with backup enabled are managed. Known
Wine user paths map to Windows Roaming AppData, Local AppData, LocalLow,
Documents or Saved Games using `SHGetKnownFolderPath`. Paths in the game itself
remain game-local. Unknown or overlapping mappings block preparation instead
of silently dropping saves. This is not a virtual filesystem: undeclared files,
registry changes, services, and other game side effects are not intercepted.

For declared external state, a native session:

1. Verifies the runtime, contract and executable; rejects links, path escapes,
   overlapping destinations, insufficient baseline staging space and concurrent
   OGV sessions for the user/materialization.
2. Records original host state, preserves it by renaming it beside its original
   path, and stages the materialized state in the game's real Windows folder.
   An absent materialized save is an empty seed, never the host's existing save.
3. Starts the executable with preserved arguments and working directory. A
   Windows Job Object owns the ordinary child process tree; capture waits for
   descendants too. Closing the launcher kills that tree and leaves recovery
   evidence rather than permitting a background child to write after capture.
4. Copies and verifies the resulting state into the materialized prefix, then
   restores the original host files. The next launch uses this latest progress,
   not the initial Vault backup. Successful cleanup retains a small receipt,
   not another permanent copy of all save files.

An existing `active.json` blocks another native launch and the generated Linux
Play/Remove scripts. Run `RECUPERAR_WINDOWS.bat` on the same Windows host and user
before moving/removing that directory or returning to Linux. Recovery does not
need an intact game executable. Never delete the journal to bypass recovery.
The original host state may still be held in its session-specific sibling path.
Recovery does not run a game or accept a new baseline into the Vault.

Process interruption recovery is journaled; full power-loss durability still
depends on the filesystem/storage. Simultaneous writes by a different program,
cloud synchronization, and a full disk after unbounded save growth can require
manual recovery. Verified staging keeps evidence when it cannot complete.

Identity is state too: a declared `configs.user.ini` in
`drive_c/users/steamuser/AppData/Roaming/GSE Saves/settings/` maps to the current
Windows user's corresponding folder. Its preserved bytes carry account and
language across platforms. If a declared GSE identity file names a different
account from the declared account-specific save, preparation reports a mismatch.
Existing capsules must actually declare/preserve this configuration; the native
launcher cannot reconstruct missing identity from a game executable.

## Prerequisites and registry

A Wine/Bottles prefix is not a native Windows installation. This release never
imports `system.reg`, `user.reg`, Wine system DLLs or DXVK into Windows. Games
that require registry values, VC++ runtimes, legacy DirectX components,
launchers, services or drivers still need those native requirements. Neither a
successful preparation nor an executable header check proves their presence.
A failed native process launch reports its error and retains/restores state
through the session lifecycle.

Processes started through external services or WMI are not owned by the game's
Job Object; games requiring that launch model need a separate reviewed recipe.

No registry virtualization is claimed. `RegOverridePredefKey` affects the
calling process; setting it in a launcher would not transparently redirect an
unmodified game's registry. A future narrowly scoped per-game Windows recipe
can describe missing prerequisites or explicit configuration without replacing
the archived game object. Such a recipe is not implemented in this release.
Windows uses host networking; Linux network isolation is not reproduced.

## Verification

`tests/test_windows_launch.py` exercises portable metadata derivation, PE
headers, account mismatches, path collisions, mutable-state exclusions and Linux
recovery guards. `tests/test_composition.py` covers existing neutral Wine,
Bottles and UMU compositions, original capsule stability and the shared game
file identity. The tests use small synthetic archives.

`tests_windows/test_native_runtime.py` compiles a real PE probe on Windows and
executes the shipped PowerShell runtime. It tests native process launch,
arguments, working directory, identity variables, repeated progress, host state
restoration, empty seeds, descendant processes, nonzero exits, integrity errors,
junction rejection and forced-launcher-kill recovery. Mutations use unique,
test-owned AppData paths. The CI matrix is in `.github/workflows/validate.yml`.

These native tests must pass before this experimental launcher is promoted.
They do not test game graphics/audio, actual GSE behavior or commercial game
compatibility. Each game's functional acceptance needs a real Windows run with
its native prerequisites and a save/load round trip.

## Platform references

- [Microsoft: Windows Known Folders](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)
- [Microsoft: job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Microsoft: registry redirection is process-local](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regoverridepredefkey)
