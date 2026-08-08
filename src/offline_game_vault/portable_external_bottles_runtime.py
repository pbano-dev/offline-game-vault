"""Portable operations for an external Bottles materialization."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
from typing import Any, Sequence

RECEIPT_NAME = ".ogv-bottles-deployment.json"
DEFAULT_FLATPAK_APP = "com.usebottles.bottles"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class PortableExternalBottlesError(RuntimeError):
    pass


def _root_path(value: Path) -> Path:
    root = value.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise PortableExternalBottlesError(
            "Materialization root must be an existing regular directory."
        )
    resolved = root.resolve()
    if resolved in {Path(resolved.anchor), Path.home().resolve()}:
        raise PortableExternalBottlesError("Unsafe materialization root.")
    return resolved


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise PortableExternalBottlesError(
            f"{field} is not a portable relative path."
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise PortableExternalBottlesError(
            f"{field} is not a safe relative path."
        )
    return path


def _path_under(root: Path, value: Any, field: str) -> Path:
    relative = _safe_relative(value, field)
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise PortableExternalBottlesError(
            f"{field} escapes the materialization."
        ) from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableExternalBottlesError(f"{label} is absent.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableExternalBottlesError(
            f"{label} is invalid: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PortableExternalBottlesError(
            f"{label} must contain an object."
        )
    return value


def _documents(output: str) -> list[Any]:
    cleaned = _ANSI_ESCAPE.sub("", output)
    candidates = [cleaned.strip()]
    candidates.extend(
        line.strip()
        for line in cleaned.splitlines()
        if line.strip().startswith(("{", "[", '"'))
    )
    result: list[Any] = []
    seen: set[str] = set()
    for candidate in reversed(candidates):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return result


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _flatpak_info(flatpak_app: str) -> None:
    if not isinstance(flatpak_app, str) or not flatpak_app:
        raise PortableExternalBottlesError(
            "Flatpak application ID is invalid."
        )
    try:
        process = subprocess.run(
            ["flatpak", "info", flatpak_app],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise PortableExternalBottlesError(
            "flatpak executable was not found."
        ) from exc
    if process.returncode != 0:
        detail = process.stderr.strip()
        raise PortableExternalBottlesError(
            f"Bottles Flatpak {flatpak_app!r} is not available"
            + (f": {detail[-500:]}" if detail else ".")
        )


def _flatpak_cli(
    *arguments: str,
    flatpak_app: str,
    filesystem_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["flatpak", "run"]
    if filesystem_root is not None:
        command.append(f"--filesystem={filesystem_root}")
    command.extend(
        [
            "--unshare=network",
            "--command=bottles-cli",
            flatpak_app,
            *arguments,
        ]
    )
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise PortableExternalBottlesError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise PortableExternalBottlesError(
            f"Cannot invoke Bottles CLI: {exc}"
        ) from exc


def _discover_bottles_path(flatpak_app: str) -> Path:
    attempts = (
        ("--json", "info", "bottles-path"),
        ("info", "bottles-path"),
    )
    diagnostics: list[str] = []
    for arguments in attempts:
        process = _flatpak_cli(
            *arguments,
            flatpak_app=flatpak_app,
        )
        if process.returncode != 0:
            diagnostics.append(
                f"{' '.join(arguments)} exited with {process.returncode}"
            )
            continue
        raw: list[str] = []
        for document in _documents(process.stdout):
            raw.extend(_walk_strings(document))
        raw.extend(_ANSI_ESCAPE.sub("", process.stdout).splitlines())
        candidates: list[Path] = []
        for item in raw:
            value = item.strip().strip("'\"")
            if value.startswith("file://"):
                value = value[7:]
            if not value.startswith("/"):
                continue
            candidate = Path(value)
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and resolved not in candidates:
                candidates.append(resolved)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise PortableExternalBottlesError(
                "Bottles CLI returned multiple managed path candidates."
            )
        diagnostics.append(
            f"{' '.join(arguments)} returned no existing absolute directory"
        )
    raise PortableExternalBottlesError(
        "Cannot discover Bottles managed path via bottles-cli. "
        + " | ".join(diagnostics)
    )


def _enumerated_names(output: str) -> set[str]:
    names: set[str] = set()
    for document in _documents(output):
        if isinstance(document, dict):
            raw = document.get("bottles")
            if isinstance(raw, list):
                names.update(
                    item.strip()
                    for item in raw
                    if isinstance(item, str) and item.strip()
                )
            names.update(
                key.strip()
                for key, value in document.items()
                if (
                    isinstance(key, str)
                    and key.strip()
                    and isinstance(value, dict)
                )
            )
        elif isinstance(document, list):
            names.update(
                item.strip()
                for item in document
                if isinstance(item, str) and item.strip()
            )
    for line in _ANSI_ESCAPE.sub("", output).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            name = stripped[2:].strip()
            if name:
                names.add(name)
    return names


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise PortableExternalBottlesError(
                "Invalid quoted bottle.yml value."
            ) from exc
    return value


def _bottle_fields(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableExternalBottlesError("bottle.yml is absent.")
    wanted = {"Name", "Path", "Custom_Path", "Runner"}
    result: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PortableExternalBottlesError(
            f"bottle.yml cannot be read: {exc}"
        ) from exc
    for line in lines:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in wanted and key not in result:
            result[key] = _parse_scalar(raw)
    missing = sorted(wanted - set(result))
    if missing:
        raise PortableExternalBottlesError(
            "bottle.yml lacks required fields: " + ", ".join(missing)
        )
    return result


def _receipt(root: Path) -> dict[str, Any]:
    receipt = _load_json(root / RECEIPT_NAME, "Bottles receipt")
    if receipt.get("schema") != 0 or receipt.get("destination") != ".":
        raise PortableExternalBottlesError(
            "Unsupported or unanchored Bottles receipt."
        )
    if receipt.get("adapter") != "bottles-flatpak":
        raise PortableExternalBottlesError(
            "Receipt is not a Bottles deployment."
        )
    name = receipt.get("bottle_name")
    if not isinstance(name, str) or _SAFE_NAME.fullmatch(name) is None:
        raise PortableExternalBottlesError(
            "Receipt has an invalid bottle_name."
        )
    layout = receipt.get("layout")
    if (
        not isinstance(layout, dict)
        or layout.get("kind") != "external-wrapper-v1"
    ):
        raise PortableExternalBottlesError(
            "Receipt is not an external Bottles materialization."
        )
    return receipt


def _layout_paths(
    root: Path,
    receipt: dict[str, Any],
) -> tuple[Path, Path, Path]:
    layout = receipt["layout"]
    prefix = _path_under(root, layout.get("prefix"), "layout.prefix")
    game = _path_under(root, layout.get("game"), "layout.game")
    registration_target = _path_under(
        root,
        layout.get("registration_target"),
        "layout.registration_target",
    )
    if registration_target != prefix:
        raise PortableExternalBottlesError(
            "Registration target does not match the prefix."
        )
    if prefix.is_symlink() or not prefix.is_dir():
        raise PortableExternalBottlesError(
            "External Bottles prefix is absent."
        )
    if game.is_symlink() or not game.is_dir():
        raise PortableExternalBottlesError(
            "External game payload is absent."
        )
    return prefix, game, registration_target


def _registration(
    managed: Path,
    receipt: dict[str, Any],
) -> Path:
    return managed / str(receipt["bottle_name"])


def _registration_matches(path: Path, expected: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=True) == expected.resolve(strict=True)
    except OSError:
        return False


def _ensure_registration(
    managed: Path,
    receipt: dict[str, Any],
    target: Path,
) -> Path:
    registration = _registration(managed, receipt)
    if registration.is_symlink():
        if not _registration_matches(registration, target):
            raise PortableExternalBottlesError(
                "Bottle name is registered to another target."
            )
        return registration
    if registration.exists():
        raise PortableExternalBottlesError(
            "Bottle name collides with a managed directory."
        )
    try:
        registration.symlink_to(target, target_is_directory=True)
    except FileExistsError as exc:
        raise PortableExternalBottlesError(
            "Bottle registration appeared concurrently."
        ) from exc
    except OSError as exc:
        raise PortableExternalBottlesError(
            f"Cannot create Bottles registration: {exc}"
        ) from exc
    return registration


def _assert_enumerated(
    *,
    root: Path,
    flatpak_app: str,
    bottle_name: str,
) -> None:
    diagnostics: list[str] = []
    for arguments in (
        ("--json", "list", "bottles"),
        ("list", "bottles"),
    ):
        process = _flatpak_cli(
            *arguments,
            flatpak_app=flatpak_app,
            filesystem_root=root,
        )
        if process.returncode != 0:
            diagnostics.append(
                f"{' '.join(arguments)} exited with {process.returncode}"
            )
            continue
        if bottle_name in _enumerated_names(process.stdout):
            return
        diagnostics.append(
            f"{' '.join(arguments)} did not enumerate {bottle_name!r}"
        )
    raise PortableExternalBottlesError(
        "Bottles did not enumerate the external registration. "
        + " | ".join(diagnostics)
    )


def _verify(
    root: Path,
    *,
    flatpak_app: str,
    require_registration: bool,
    require_enumeration: bool,
) -> dict[str, Any]:
    canonical = _root_path(root)
    _flatpak_info(flatpak_app)
    managed = _discover_bottles_path(flatpak_app)
    receipt = _receipt(canonical)
    prefix, game, registration_target = _layout_paths(
        canonical,
        receipt,
    )
    bottle_name = str(receipt["bottle_name"])
    fields = _bottle_fields(prefix / "bottle.yml")
    if (
        fields["Name"] != bottle_name
        or fields["Path"] != bottle_name
        or fields["Custom_Path"] is not False
        or fields["Runner"] != receipt.get("runner")
    ):
        raise PortableExternalBottlesError(
            "Bottle identity or runner changed."
        )

    launch = receipt.get("launch")
    if not isinstance(launch, dict):
        raise PortableExternalBottlesError(
            "Receipt has no launch contract."
        )
    entrypoint = _path_under(
        canonical,
        launch.get("entrypoint"),
        "launch.entrypoint",
    )
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise PortableExternalBottlesError(
            "Game entrypoint is absent."
        )
    try:
        entrypoint.relative_to(game)
    except ValueError as exc:
        raise PortableExternalBottlesError(
            "Game entrypoint is outside payload/game."
        ) from exc

    paths = receipt.get("operational_paths")
    if not isinstance(paths, dict):
        raise PortableExternalBottlesError(
            "Receipt has no operational_paths."
        )
    checked: dict[str, str] = {}
    for key in (
        "launcher",
        "verifier",
        "uninstaller",
        "portable_runtime",
    ):
        path = _path_under(
            canonical,
            paths.get(key),
            f"operational_paths.{key}",
        )
        if path.is_symlink() or not path.is_file():
            raise PortableExternalBottlesError(
                f"Operational file is absent: {key}"
            )
        if key != "portable_runtime" and not os.access(path, os.X_OK):
            raise PortableExternalBottlesError(
                f"Operational script is not executable: {key}"
            )
        checked[key] = str(path.relative_to(canonical))

    runner = receipt.get("runner")
    if not isinstance(runner, str) or not runner:
        raise PortableExternalBottlesError(
            "Receipt runner is invalid."
        )
    runner_path = managed.parent / "runners" / runner
    if runner_path.is_symlink() or not runner_path.is_dir():
        raise PortableExternalBottlesError(
            "The exact Bottles runner is not installed."
        )

    registration = _registration(managed, receipt)
    registration_valid = _registration_matches(
        registration,
        registration_target,
    )
    if require_registration and not registration_valid:
        if registration.exists() and not registration.is_symlink():
            detail = "a non-link entry occupies the bottle name"
        elif registration.is_symlink():
            detail = "the link targets another materialization"
        else:
            detail = "the registration is absent"
        raise PortableExternalBottlesError(
            f"External Bottles registration is invalid: {detail}."
        )
    if require_enumeration:
        _assert_enumerated(
            root=canonical,
            flatpak_app=flatpak_app,
            bottle_name=bottle_name,
        )

    return {
        "schema": 0,
        "capsule_id": receipt.get("capsule_id"),
        "profile_id": receipt.get("profile_id"),
        "backend": "bottles",
        "destination": ".",
        "bottle_name": bottle_name,
        "runner": runner,
        "entrypoint": str(entrypoint.relative_to(canonical)),
        "prefix": str(prefix.relative_to(canonical)),
        "game": str(game.relative_to(canonical)),
        "registration": str(registration),
        "registration_valid": registration_valid,
        "runner_present": True,
        "operational_paths": checked,
        "verified": True,
        "complete": True,
    }


def verify(
    root: Path,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> dict[str, Any]:
    return _verify(
        root,
        flatpak_app=flatpak_app,
        require_registration=True,
        require_enumeration=True,
    )


def _play_command(
    root: Path,
    *,
    flatpak_app: str,
    arguments: Sequence[str],
) -> tuple[list[str], dict[str, Any]]:
    canonical = _root_path(root)
    _flatpak_info(flatpak_app)
    managed = _discover_bottles_path(flatpak_app)
    receipt = _receipt(canonical)
    _prefix, _game, registration_target = _layout_paths(
        canonical,
        receipt,
    )
    _ensure_registration(managed, receipt, registration_target)
    _assert_enumerated(
        root=canonical,
        flatpak_app=flatpak_app,
        bottle_name=str(receipt["bottle_name"]),
    )
    verification = _verify(
        canonical,
        flatpak_app=flatpak_app,
        require_registration=True,
        require_enumeration=False,
    )
    launch = receipt["launch"]
    entrypoint = _path_under(
        canonical,
        launch.get("entrypoint"),
        "launch.entrypoint",
    )
    command = ["flatpak", "run", f"--filesystem={canonical}"]
    if launch.get("network") == "isolated":
        command.append("--unshare=network")
    command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            str(receipt["bottle_name"]),
            "-e",
            str(entrypoint),
        ]
    )
    configured = launch.get("arguments", [])
    if not isinstance(configured, list) or any(
        not isinstance(item, str) for item in configured
    ):
        raise PortableExternalBottlesError(
            "Configured launch arguments are invalid."
        )
    combined = [*configured, *arguments]
    if combined:
        command.append("--")
        command.extend(combined)
    return command, verification


def play_exec(
    root: Path,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
    arguments: Sequence[str] = (),
) -> None:
    command, _verification = _play_command(
        root,
        flatpak_app=flatpak_app,
        arguments=arguments,
    )
    try:
        os.execvp(command[0], command)
    except FileNotFoundError as exc:
        raise PortableExternalBottlesError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise PortableExternalBottlesError(
            f"Cannot execute Bottles Flatpak: {exc}"
        ) from exc


def uninstall(
    root: Path,
    *,
    confirm_stopped: bool,
    confirm_state_preserved: bool,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> dict[str, Any]:
    canonical = _root_path(root)
    if not confirm_stopped:
        raise PortableExternalBottlesError(
            "Removal requires --confirm-stopped after closing Bottles "
            "and the game."
        )
    verification = _verify(
        canonical,
        flatpak_app=flatpak_app,
        require_registration=False,
        require_enumeration=False,
    )
    receipt = _receipt(canonical)
    persistent = receipt.get("persistent_state", [])
    if not isinstance(persistent, list):
        raise PortableExternalBottlesError(
            "Receipt persistent_state is invalid."
        )
    must_preserve = [
        item
        for item in persistent
        if (
            isinstance(item, dict)
            and item.get("preserve_on_remove") is True
        )
    ]
    if must_preserve and not confirm_state_preserved:
        raise PortableExternalBottlesError(
            "Persistent state must be preserved before removal."
        )

    managed = _discover_bottles_path(flatpak_app)
    _prefix, _game, registration_target = _layout_paths(
        canonical,
        receipt,
    )
    registration = _registration(managed, receipt)
    registration_removed = False
    if registration.is_symlink():
        if not _registration_matches(registration, registration_target):
            raise PortableExternalBottlesError(
                "Refusing removal: registration targets another materialization."
            )
        registration.unlink()
        registration_removed = True
    elif registration.exists():
        raise PortableExternalBottlesError(
            "Refusing removal: bottle name is occupied by a regular entry."
        )

    detached = canonical.parent / (
        f".ogv-remove-{canonical.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    if detached.exists() or detached.is_symlink():
        raise PortableExternalBottlesError(
            "Removal staging path already exists."
        )
    os.rename(canonical, detached)
    try:
        shutil.rmtree(detached)
    except OSError as exc:
        if detached.exists() and not canonical.exists():
            try:
                os.rename(detached, canonical)
                if registration_removed:
                    registration.symlink_to(
                        registration_target,
                        target_is_directory=True,
                    )
            except OSError:
                pass
        raise PortableExternalBottlesError(
            "Materialization was detached but not removed."
        ) from exc
    return {
        "schema": 0,
        "capsule_id": verification["capsule_id"],
        "profile_id": verification["profile_id"],
        "backend": "bottles",
        "destination": ".",
        "bottle_name": verification["bottle_name"],
        "registration_removed": registration_removed,
        "state_preservation_confirmed": confirm_state_preserved,
        "stopped_confirmed": confirm_stopped,
        "removed": True,
        "complete": True,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ogv-external-bottles-runtime"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument(
        "--flatpak-app",
        default=DEFAULT_FLATPAK_APP,
    )
    verify_parser.add_argument("--json", action="store_true")

    play_parser = commands.add_parser("play")
    play_parser.add_argument("--root", type=Path, required=True)
    play_parser.add_argument(
        "--flatpak-app",
        default=DEFAULT_FLATPAK_APP,
    )
    play_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    uninstall_parser = commands.add_parser("uninstall")
    uninstall_parser.add_argument("--root", type=Path, required=True)
    uninstall_parser.add_argument(
        "--flatpak-app",
        default=DEFAULT_FLATPAK_APP,
    )
    uninstall_parser.add_argument(
        "--confirm-stopped",
        action="store_true",
    )
    uninstall_parser.add_argument(
        "--confirm-state-preserved",
        action="store_true",
    )
    uninstall_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(
                args.root,
                flatpak_app=args.flatpak_app,
            )
            if args.json:
                _print(result)
            else:
                for key, value in result.items():
                    print(f"{key}: {value}")
            return 0
        if args.command == "play":
            extra = list(args.arguments)
            if extra and extra[0] == "--":
                extra = extra[1:]
            play_exec(
                args.root,
                flatpak_app=args.flatpak_app,
                arguments=extra,
            )
            raise AssertionError("os.execvp unexpectedly returned")
        if args.command == "uninstall":
            result = uninstall(
                args.root,
                confirm_stopped=args.confirm_stopped,
                confirm_state_preserved=args.confirm_state_preserved,
                flatpak_app=args.flatpak_app,
            )
            if args.json:
                _print(result)
            else:
                for key, value in result.items():
                    print(f"{key}: {value}")
            return 0
        raise PortableExternalBottlesError("Unknown command.")
    except PortableExternalBottlesError as exc:
        print(
            f"ogv-external-bottles-runtime: error: {exc}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
