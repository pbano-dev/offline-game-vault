"""Self-contained operational runtime for Bottles materializations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
from typing import Any, Sequence

RECEIPT_NAME = ".ogv-bottles-deployment.json"
LAST_RUN_RECEIPT = "receipts/last-bottles-run.json"
DEFAULT_FLATPAK_APP = "com.usebottles.bottles"


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _flatpak_cli(
    *arguments: str,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> subprocess.CompletedProcess[str]:
    command = [
        "flatpak",
        "run",
        "--unshare=network",
        "--command=bottles-cli",
        flatpak_app,
        *arguments,
    ]
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
        raise PortableBottlesError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise PortableBottlesError(
            f"Cannot invoke Bottles CLI: {exc}"
        ) from exc


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


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


def _discover_bottles_path(
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> Path:
    attempts = (
        ("--json", "info", "bottles-path"),
        ("info", "bottles-path"),
    )
    diagnostics: list[str] = []
    for arguments in attempts:
        process = _flatpak_cli(*arguments, flatpak_app=flatpak_app)
        if process.returncode != 0:
            diagnostics.append(
                f"{' '.join(arguments)} exited with {process.returncode}"
            )
            continue
        raw: list[str] = []
        for document in _documents(process.stdout):
            raw.extend(_walk_strings(document))
        raw.extend(
            _ANSI_ESCAPE.sub("", process.stdout).splitlines()
        )
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
            raise PortableBottlesError(
                "Bottles CLI returned multiple managed path candidates."
            )
        diagnostics.append(
            f"{' '.join(arguments)} returned no existing absolute directory"
        )
    raise PortableBottlesError(
        "Cannot discover Bottles managed path via bottles-cli. "
        + " | ".join(diagnostics)
    )


def _require_managed_root(
    root: Path,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> Path:
    canonical = _root_path(root)
    managed = _discover_bottles_path(flatpak_app=flatpak_app)
    if canonical.parent != managed:
        raise PortableBottlesError(
            "Bottle is not located in the active Bottles managed directory."
        )
    return canonical


class PortableBottlesError(RuntimeError):
    pass


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _root_path(value: Path) -> Path:
    root = value.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise PortableBottlesError(
            "Bottle root must be an existing regular directory."
        )
    resolved = root.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise PortableBottlesError("Unsafe bottle root.")
    return resolved


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise PortableBottlesError(f"{field} is not a portable relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableBottlesError(f"{field} is not a safe relative path.")
    return path


def _path_under(root: Path, value: Any, field: str) -> Path:
    relative = _safe_relative(value, field)
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PortableBottlesError(f"{field} escapes the bottle.") from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableBottlesError(f"{label} is absent.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PortableBottlesError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise PortableBottlesError(f"{label} must contain an object.")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
            raise PortableBottlesError("Invalid quoted bottle.yml value.") from exc
    return value


def _bottle_fields(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableBottlesError("bottle.yml is absent.")
    wanted = {"Name", "Path", "Custom_Path", "Runner"}
    result: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise PortableBottlesError("bottle.yml is not UTF-8.") from exc
    for line in lines:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in wanted and key not in result:
            result[key] = _parse_scalar(raw)
    missing = sorted(wanted - set(result))
    if missing:
        raise PortableBottlesError(
            "bottle.yml lacks required fields: " + ", ".join(missing)
        )
    return result


def _receipt(root: Path) -> dict[str, Any]:
    receipt = _load_json(root / RECEIPT_NAME, "Bottles receipt")
    if receipt.get("schema") != 0 or receipt.get("destination") != ".":
        raise PortableBottlesError("Unsupported or unanchored Bottles receipt.")
    if receipt.get("adapter") != "bottles-flatpak":
        raise PortableBottlesError("Receipt is not a Bottles deployment.")
    if receipt.get("bottle_name") != root.name:
        raise PortableBottlesError("Receipt bottle_name does not match the directory.")
    return receipt


def verify(
    root: Path,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> dict[str, Any]:
    canonical = _require_managed_root(
        root,
        flatpak_app=flatpak_app,
    )
    receipt = _receipt(canonical)
    fields = _bottle_fields(canonical / "bottle.yml")
    if (
        fields["Name"] != canonical.name
        or fields["Path"] != canonical.name
        or fields["Custom_Path"] is not False
        or fields["Runner"] != receipt.get("runner")
    ):
        raise PortableBottlesError("Bottle identity or runner changed.")

    launch = receipt.get("launch")
    if not isinstance(launch, dict):
        raise PortableBottlesError("Receipt has no launch contract.")
    entrypoint = _path_under(
        canonical, launch.get("entrypoint"), "launch.entrypoint"
    )
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise PortableBottlesError("Game entrypoint is absent.")

    paths = receipt.get("operational_paths")
    if not isinstance(paths, dict):
        raise PortableBottlesError("Receipt has no operational_paths.")
    checked: dict[str, str] = {}
    for key in ("launcher", "verifier", "uninstaller", "portable_runtime"):
        path = _path_under(canonical, paths.get(key), f"operational_paths.{key}")
        if path.is_symlink() or not path.is_file():
            raise PortableBottlesError(f"Operational file is absent: {key}")
        if key != "portable_runtime" and not os.access(path, os.X_OK):
            raise PortableBottlesError(f"Operational script is not executable: {key}")
        checked[key] = str(path.relative_to(canonical))

    return {
        "schema": 0,
        "capsule_id": receipt.get("capsule_id"),
        "profile_id": receipt.get("profile_id"),
        "backend": "bottles",
        "destination": ".",
        "bottle_name": canonical.name,
        "runner": receipt.get("runner"),
        "entrypoint": str(entrypoint.relative_to(canonical)),
        "operational_paths": checked,
        "verified": True,
        "complete": True,
    }


def play(
    root: Path,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
    arguments: Sequence[str] = (),
) -> dict[str, Any]:
    canonical = _root_path(root)
    verification = verify(canonical, flatpak_app=flatpak_app)
    receipt = _receipt(canonical)
    launch = receipt["launch"]
    entrypoint = _path_under(
        canonical, launch.get("entrypoint"), "launch.entrypoint"
    )
    command = ["flatpak", "run"]
    if launch.get("network") == "isolated":
        command.append("--unshare=network")
    command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            canonical.name,
            "-e",
            str(entrypoint),
        ]
    )
    configured = launch.get("arguments", [])
    if not isinstance(configured, list) or any(
        not isinstance(item, str) for item in configured
    ):
        raise PortableBottlesError("Configured launch arguments are invalid.")
    combined = [*configured, *arguments]
    if combined:
        command.append("--")
        command.extend(combined)
    try:
        process = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise PortableBottlesError("flatpak executable was not found.") from exc
    result = {
        "schema": 0,
        "capsule_id": verification["capsule_id"],
        "profile_id": verification["profile_id"],
        "backend": "bottles",
        "destination": ".",
        "bottle_name": canonical.name,
        "runner": verification["runner"],
        "network": launch.get("network", "host_default"),
        "process_rc": int(process.returncode),
        "created_at": _now(),
        "complete": process.returncode == 0,
    }
    _write_json_atomic(canonical / LAST_RUN_RECEIPT, result)
    return result


def uninstall(
    root: Path,
    *,
    confirm_stopped: bool,
    confirm_state_preserved: bool,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> dict[str, Any]:
    canonical = _root_path(root)
    verification = verify(canonical, flatpak_app=flatpak_app)
    receipt = _receipt(canonical)
    if not confirm_stopped:
        raise PortableBottlesError(
            "Removal requires --confirm-stopped after closing Bottles and the game."
        )
    persistent = receipt.get("persistent_state", [])
    if not isinstance(persistent, list):
        raise PortableBottlesError("Receipt persistent_state is invalid.")
    must_preserve = [
        item for item in persistent
        if isinstance(item, dict) and item.get("preserve_on_remove") is True
    ]
    if must_preserve and not confirm_state_preserved:
        raise PortableBottlesError(
            "Persistent state must be preserved before removal."
        )

    detached = canonical.parent / (
        f".ogv-remove-{canonical.name}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    if detached.exists() or detached.is_symlink():
        raise PortableBottlesError("Removal staging path already exists.")
    os.rename(canonical, detached)
    try:
        shutil.rmtree(detached)
    except OSError as exc:
        if detached.exists() and not canonical.exists():
            try:
                os.rename(detached, canonical)
            except OSError:
                pass
        raise PortableBottlesError("Bottle was detached but not removed.") from exc
    return {
        "schema": 0,
        "capsule_id": verification["capsule_id"],
        "profile_id": verification["profile_id"],
        "backend": "bottles",
        "destination": ".",
        "bottle_name": verification["bottle_name"],
        "state_preservation_confirmed": confirm_state_preserved,
        "stopped_confirmed": confirm_stopped,
        "removed": True,
        "complete": True,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ogv-bottles-runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--flatpak-app", default=DEFAULT_FLATPAK_APP)
    verify_parser.add_argument("--json", action="store_true")

    play_parser = commands.add_parser("play")
    play_parser.add_argument("--root", type=Path, required=True)
    play_parser.add_argument("--flatpak-app", default=DEFAULT_FLATPAK_APP)
    play_parser.add_argument("--json", action="store_true")
    play_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    uninstall_parser = commands.add_parser("uninstall")
    uninstall_parser.add_argument("--root", type=Path, required=True)
    uninstall_parser.add_argument("--flatpak-app", default=DEFAULT_FLATPAK_APP)
    uninstall_parser.add_argument("--confirm-stopped", action="store_true")
    uninstall_parser.add_argument(
        "--confirm-state-preserved", action="store_true"
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
        elif args.command == "play":
            extra = list(args.arguments)
            if extra and extra[0] == "--":
                extra = extra[1:]
            result = play(
                args.root,
                flatpak_app=args.flatpak_app,
                arguments=extra,
            )
        elif args.command == "uninstall":
            result = uninstall(
                args.root,
                confirm_stopped=args.confirm_stopped,
                confirm_state_preserved=args.confirm_state_preserved,
                flatpak_app=args.flatpak_app,
            )
        else:
            raise PortableBottlesError("Unknown command.")
        if args.json:
            _print(result)
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        if args.command == "play":
            return int(result["process_rc"])
        return 0
    except PortableBottlesError as exc:
        print(f"ogv-bottles-runtime: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
