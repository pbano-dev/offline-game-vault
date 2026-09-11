"""Derive native Windows launch metadata without changing a Vault capsule.

This module is portable stdlib Python. The generated runtime needs Windows
PowerShell 5.1, not a system installation of OGV or Python. Preparation is not
functional acceptance. Unsupported layouts are reported without breaking Linux.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any

from . import __version__

CONTRACT = "ogv-windows-launch-v1"
DIRECTORY = Path("metadata/windows")
MANIFEST = DIRECTORY / "launch.json"
FILE_MANIFEST = DIRECTORY / "files.json"
RUNTIME = DIRECTORY / "runtime.ps1"
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
FOLDERS = {
    ("AppData", "Roaming"): "RoamingAppData",
    ("AppData", "Local"): "LocalAppData",
    ("AppData", "LocalLow"): "LocalAppDataLow",
    ("Documents",): "Documents",
    ("My Documents",): "Documents",
    ("Saved Games",): "SavedGames",
}
OPERATIONS = {
    "JUGAR_WINDOWS.bat": "Play",
    "VERIFICAR_WINDOWS.bat": "Verify",
    "RECUPERAR_WINDOWS.bat": "Recover",
}


class WindowsLaunchError(ValueError):
    pass


def relative(value: Any, label: str = "path") -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise WindowsLaunchError(f"{label}: expected a relative portable path")
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or path.as_posix() != value
            or any(p in {".", ".."} for p in path.parts)):
        raise WindowsLaunchError(f"{label}: unsafe path {value!r}")
    for part in path.parts:
        if (part.endswith((" ", ".")) or RESERVED.match(part)
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            raise WindowsLaunchError(f"{label}: Windows cannot represent {part!r}")
    return path


def contained(root: Path, value: str, *, exists: bool = False) -> Path:
    path = root.joinpath(*relative(value).parts)
    # Linux's internal prefix links may identify the shared game. Resolve them
    # now and serialize their contained physical target for Windows.
    resolved = path.resolve(strict=exists)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WindowsLaunchError("Path escapes the materialization") from exc
    relative(resolved.relative_to(root).as_posix())
    return resolved


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WindowsLaunchError(f"Missing regular metadata: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WindowsLaunchError(f"{path.name}: expected an object")
    return value


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    path.with_name(path.name + ".sha256").write_text(_hash(path) + "\n", encoding="ascii")


def _regular_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    paths = [path]
    if path.is_dir():
        for base, dirs, files in os.walk(path, followlinks=False):
            seen: set[str] = set()
            for name in dirs + files:
                relative(name)
                if name.casefold() in seen:
                    raise WindowsLaunchError(f"Case-insensitive filename collision: {name!r}")
                seen.add(name.casefold())
                paths.append(Path(base) / name)
    for item in paths:
        mode = item.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise WindowsLaunchError(f"Windows tree contains a link or special file: {item.name}")


def state_mapping(declaration: dict[str, Any]) -> tuple[str, str]:
    parts = relative(declaration.get("path"), "persistent_state.path").parts
    if len(parts) > 4 and parts[:2] == ("drive_c", "users"):
        tail = parts[3:]
        for prefix, folder in FOLDERS.items():
            if tuple(p.casefold() for p in tail[:len(prefix)]) == tuple(p.casefold() for p in prefix):
                subpath = "/".join(tail[len(prefix):])
                # Never replace a whole user directory.
                relative(subpath, "state destination")
                return folder, subpath
    raise WindowsLaunchError(
        f"State {declaration.get('id')!r} has no known Windows folder mapping"
    )


def _profile(capsule: dict[str, Any], profile_id: str) -> dict[str, Any]:
    matches = [p for p in capsule.get("profiles", []) if p.get("id") == profile_id]
    if len(matches) != 1:
        raise WindowsLaunchError("No unique operational profile")
    return matches[0]


def _paths(root: Path, profile: dict[str, Any], entry_in_game: str | None = None) -> tuple[Path, Path, Path, Path]:
    adapter = profile.get("adapter")
    launch = profile.get("launch", {})
    if adapter == "bottles":
        receipt = _load(root / ".ogv-bottles-deployment.json")
        layout = receipt.get("layout", {})
        if layout.get("kind") != "external-wrapper-v1":
            raise WindowsLaunchError("Bottles needs an external-wrapper-v1 materialization")
        prefix = contained(root, layout["prefix"], exists=True)
        game = contained(root, layout["game"], exists=True)
        executable = contained(root, receipt["launch"]["entrypoint"], exists=True)
        logical_game = PurePosixPath(layout["game_destination_in_prefix"])
        wd = PurePosixPath(launch.get("working_directory") or logical_game.as_posix())
        try:
            work = game.joinpath(*wd.relative_to(logical_game).parts)
        except ValueError as exc:
            raise WindowsLaunchError("Bottles working directory is outside its game") from exc
    else:
        spec = profile.get("playable") if adapter == "wine" else profile.get("umu")
        if not isinstance(spec, dict):
            raise WindowsLaunchError("No supported native game layout")
        prefix = contained(root, spec.get("paths", {}).get("prefix"), exists=True)
        executable = contained(root, launch.get("entrypoint"), exists=True)
        work = contained(root, launch.get("working_directory") or
                         PurePosixPath(launch["entrypoint"]).parent.as_posix(), exists=True)
        # A moved/linked neutral game is authoritative when declared.
        operations = spec.get("prefix_operations", []) + spec.get("prefix_moves", [])
        candidates = []
        for op in operations:
            if op.get("type") in {"move", "symlink"} and isinstance(op.get("path"), str):
                target = contained(root, op["path"])
                if target.is_dir() and executable.is_relative_to(target):
                    candidates.append(target)
        if entry_in_game:
            suffix = relative(entry_in_game).parts
            if tuple(executable.parts[-len(suffix):]) != suffix:
                raise WindowsLaunchError("Native executable differs from the source contract")
            game = executable.parents[len(suffix) - 1]
        elif candidates:
            game = max(candidates, key=lambda p: len(p.parts))
        else:
            try:
                parts = executable.relative_to(prefix / "drive_c").parts
            except ValueError:
                # Standalone source/payload/game is explicit in the contract's
                # launch paths; do not guess executable names from a scan.
                raise WindowsLaunchError("Game root needs an explicit Windows recipe")
            else:
                if len(parts) >= 3 and parts[0] in {"Games", "Program Files", "Program Files (x86)"}:
                    game = prefix / "drive_c" / parts[0] / parts[1]
                else:
                    raise WindowsLaunchError("Game root needs an explicit Windows recipe")
    for path in (prefix, game, work, executable):
        path.resolve(strict=True).relative_to(root)
        relative(path.relative_to(root).as_posix())
    if not game.is_dir() or not work.is_dir() or not executable.is_relative_to(game):
        raise WindowsLaunchError("Invalid game or working directory")
    inspect_pe(executable)
    return prefix, game, work, executable


def inspect_pe(executable: Path) -> str:
    """Read bounded PE headers, without loading or executing game code."""
    if executable.suffix.casefold() != ".exe":
        raise WindowsLaunchError("Declared launcher is not a Windows executable")
    with executable.open("rb") as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise WindowsLaunchError("Invalid Windows DOS/PE header")
        offset = struct.unpack_from("<I", dos, 60)[0]
        size = executable.stat().st_size
        if offset < 64 or offset > size - 26:
            raise WindowsLaunchError("Invalid Windows PE header offset")
        stream.seek(offset)
        coff = stream.read(24)
        if coff[:4] != b"PE\0\0":
            raise WindowsLaunchError("Invalid Windows PE signature")
        machine = struct.unpack_from("<H", coff, 4)[0]
        optional_size = struct.unpack_from("<H", coff, 20)[0]
        if optional_size < 2 or optional_size > size - offset - 24:
            raise WindowsLaunchError("Truncated Windows optional header")
        magic = struct.unpack("<H", stream.read(2))[0]
        if (machine, magic) not in {(0x14C, 0x10B), (0x8664, 0x20B)}:
            raise WindowsLaunchError("Native launcher currently supports x86 and x64 PE executables")
    return "x64" if machine == 0x8664 else "x86"


def prepare_windows_launch(
    *, destination: Path, capsule_path: Path, profile_id: str,
    state_capsule_path: Path | None = None, source_profile_id: str | None = None,
) -> dict[str, Any]:
    root = destination.expanduser().resolve(strict=True)
    target = root / DIRECTORY
    if target.exists():
        raise WindowsLaunchError("Windows launch metadata already exists")
    for name in OPERATIONS:
        if (root / name).exists() or (root / name).is_symlink():
            raise WindowsLaunchError(f"Refusing to replace {name}")
    capsule = _load(capsule_path)
    state_capsule = _load(state_capsule_path or capsule_path)
    profile = _profile(capsule, profile_id)
    doc: dict[str, Any] = {
        "schema": 0, "contract": CONTRACT, "generator": __version__,
        "capsule_id": capsule["capsule_id"], "profile_id": profile_id,
        "status": "prepared-unverified", "functional_acceptance": False,
        "network": "host_default", "registry_policy": "host-no-import",
        "issues": [], "warnings": [
            "Native Windows uses host networking; Linux network isolation is not reproduced.",
            "Wine registry and prefix system DLLs are not imported. Native dependencies must be available.",
            "Game launch, graphics, audio and save compatibility have not been accepted on Windows.",
        ],
    }
    try:
        entry_in_game = None
        if source_profile_id:
            source = _profile(state_capsule, source_profile_id)
            contract_name = source.get("host_contract")
            if contract_name:
                contract_root = (state_capsule_path or capsule_path).resolve().parent
                contract = _load(contained(contract_root, contract_name, exists=True))
                if contract.get("contract") in {
                    "ogv-game-source-v1", "ogv-bottles-neutral-v1",
                    "ogv-direct-wine-neutral-v1", "ogv-umu-neutral-v1",
                }:
                    entry_in_game = contract["entrypoint_relative_to_game"]
        prefix, game, work, exe = _paths(root, profile, entry_in_game)
        _regular_tree(game)
        declarations = state_capsule.get("persistent_state", [])
        if not isinstance(declarations, list):
            raise WindowsLaunchError("Invalid persistent-state declarations")
        states, destinations = [], []
        state_ids: set[str] = set()
        for declaration in declarations:
            if not declaration.get("backup", True):
                continue
            state_id = declaration.get("id")
            if not isinstance(state_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", state_id):
                raise WindowsLaunchError("Invalid persistent-state identifier")
            if state_id in state_ids:
                raise WindowsLaunchError("Duplicate state identifier")
            state_ids.add(state_id)
            logical = relative(declaration["path"])
            source = contained(root, (prefix.relative_to(root) / logical).as_posix())
            _regular_tree(source)
            if source.is_relative_to(game):
                folder, tail = "Game", source.relative_to(game).as_posix()
                relative(tail)
            else:
                folder, tail = state_mapping(declaration)
            key = (folder + "/" + tail).casefold()
            if any(key == p or key.startswith(p + "/") or p.startswith(key + "/") for p in destinations):
                raise WindowsLaunchError("Overlapping Windows persistent-state destinations")
            destinations.append(key)
            states.append({
                "id": state_id, "kind": declaration.get("kind"),
                "source": source.relative_to(root).as_posix(),
                "folder": folder, "path": tail,
                "initial_present": source.exists(),
            })
        arguments = profile.get("launch", {}).get("arguments", [])
        if not isinstance(arguments, list) or any(not isinstance(x, str) or "\0" in x for x in arguments):
            raise WindowsLaunchError("Unsupported launch arguments")
        # Wine/Proton environment variables are Linux instructions, not native
        # prerequisites. Preserve ordinary per-process values only.
        environment = profile.get("launch", {}).get("environment", {})
        if not isinstance(environment, dict):
            raise WindowsLaunchError("Invalid launch environment")
        native_env = {}
        reserved = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "APPDATA",
                    "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"}
        for key, value in environment.items():
            if key.upper().startswith(("WINE", "PROTON", "DXVK", "VKD3D", "UMU", "STEAM_COMPAT_", "STEAM_RUNTIME", "LD_")):
                continue
            if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key.upper() in reserved
                    or not isinstance(value, str) or "\0" in value or value.startswith("/")):
                raise WindowsLaunchError(f"Environment {key!r} needs a Windows-specific value")
            native_env[key] = value
        doc.update({
            "game_root": game.relative_to(root).as_posix(),
            "entrypoint": exe.relative_to(root).as_posix(),
            "working_directory": work.relative_to(root).as_posix(),
            "entrypoint_sha256": _hash(exe), "architecture": inspect_pe(exe), "arguments": arguments,
            "environment": native_env, "state": states,
        })
        # An identity tied to a different preserved account is not a usable
        # native configuration. Preserve original bytes, report the mismatch.
        account_ids = set()
        for state in states:
            if state["kind"] == "save":
                account_ids.update(p for p in PurePosixPath(state["path"]).parts if re.fullmatch(r"7656119\d{10}", p))
        for state in states:
            source = root / state["source"]
            if state["kind"] == "identity" and source.name.casefold() == "configs.user.ini":
                if not source.is_file():
                    raise WindowsLaunchError("Declared GSE identity is missing from the materialized state")
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(source, encoding="utf-8-sig")
                account = parser.get("user::general", "account_steamid", fallback="")
                if account_ids and account not in account_ids:
                    raise WindowsLaunchError("GSE identity differs from the preserved save account")
    except (WindowsLaunchError, OSError, KeyError, ValueError, configparser.Error) as exc:
        doc["status"] = "blocked"
        doc["issues"].append(str(exc))
    target.mkdir(parents=True)
    _write(root / MANIFEST, doc)
    runtime = Path(__file__).with_name("windows_runtime.ps1").read_bytes()
    (root / RUNTIME).write_bytes(runtime)
    (root / RUNTIME).with_suffix(".ps1.sha256").write_text(_hash(root / RUNTIME) + "\n", encoding="ascii")
    for filename, action in OPERATIONS.items():
        text = (
            '@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
            '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            '-File "%~dp0metadata\\windows\\runtime.ps1" -Action ' + action + '\r\n'
            'set "OGV_EXIT=%ERRORLEVEL%"\r\n'
            'if not "%OGV_EXIT%"=="0" pause\r\nexit /b %OGV_EXIT%\r\n'
        )
        (root / filename).write_bytes(text.encode("ascii"))
    for filename in ("JUGAR.sh", "DESINSTALAR.sh"):
        linux = root / filename
        if linux.is_file() and not linux.is_symlink():
            original = linux.read_text(encoding="utf-8")
            header, separator, body = original.partition("\n")
            if not separator or not header.startswith("#!") or "sh" not in header:
                raise WindowsLaunchError("Linux launcher has no supported shell header")
            guard = (
                'OGV_PENDING="$(dirname -- "$0")/.ogv-windows/active.json"\n'
                'if [ -e "$OGV_PENDING" ] || [ -L "$OGV_PENDING" ]; then\n'
                '  echo "Recover the interrupted Windows session on its original Windows host before playing." >&2\n'
                '  exit 1\nfi\n'
            )
            linux.write_text(header + "\n" + guard + body, encoding="utf-8")
    alias = root / "JUGAR_LINUX.sh"
    if not alias.exists():
        alias.write_text('#!/bin/sh\nexec "$(dirname -- "$0")/JUGAR.sh" "$@"\n', encoding="utf-8")
        alias.chmod(0o700)
    return {"status": doc["status"], "functional_acceptance": False,
            "issues": doc["issues"], "warnings": doc["warnings"],
            "launcher": "JUGAR_WINDOWS.bat", "manifest": MANIFEST.as_posix()}


def seal_windows_files(destination: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse the existing composition hashing pass; do not reread a 70 GB game."""
    manifest = destination / MANIFEST
    if not manifest.exists():
        return []
    doc = _load(manifest)
    game = doc.get("game_root")
    state = [s["source"] for s in doc.get("state", [])]
    selected = []
    for item in records:
        path = item["path"]
        if game and (path == game or path.startswith(game + "/")):
            if not any(path == s or path.startswith(s + "/") for s in state):
                selected.append(item)
    _write(destination / FILE_MANIFEST, {"schema": 0, "files": selected})
    result = []
    for path in (destination / FILE_MANIFEST, destination / FILE_MANIFEST.with_suffix(".json.sha256")):
        result.append({"path": path.relative_to(destination).as_posix(),
                       "sha256": _hash(path), "bytes": path.stat().st_size})
    return result
