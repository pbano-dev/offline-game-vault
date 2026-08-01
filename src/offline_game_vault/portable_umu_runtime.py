"""Self-contained operational runtime for UMU materializations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence

RECEIPT_NAME = "umu-materialization.json"
LAST_RUN_RECEIPT = "receipts/last-umu-run.json"

_RUNTIME_PLATFORM_PREFIX = {
    "steamrt2": "soldier",
    "steamrt3": "sniper",
    "steamrt4": "steamrt4",
    "steamrt4-arm64": "steamrt4-arm64",
}


class PortableUmuError(RuntimeError):
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
        raise PortableUmuError(
            "UMU root must be an existing regular directory."
        )
    resolved = root.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise PortableUmuError("Unsafe UMU materialization root.")
    return resolved


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise PortableUmuError(f"{field} is not a portable relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableUmuError(f"{field} is not a safe relative path.")
    return path


def _path_under(root: Path, value: Any, field: str) -> Path:
    relative = _safe_relative(value, field)
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PortableUmuError(f"{field} escapes the materialization.") from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableUmuError(f"{label} is absent.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PortableUmuError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise PortableUmuError(f"{label} must contain an object.")
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _receipt(root: Path) -> dict[str, Any]:
    receipt = _load_json(root / RECEIPT_NAME, "UMU receipt")
    if (
        receipt.get("schema") != 0
        or receipt.get("destination") != "."
        or receipt.get("backend") != "umu"
    ):
        raise PortableUmuError("Unsupported or unanchored UMU receipt.")
    return receipt


def _verify_offline_runtime(
    root: Path,
    receipt: dict[str, Any],
) -> dict[str, Path]:
    offline = receipt.get("offline_environment")
    if not isinstance(offline, dict):
        raise PortableUmuError(
            "UMU receipt has no verified offline_environment."
        )
    if offline.get("runtime_update") is not False:
        raise PortableUmuError("UMU runtime updates are not disabled.")

    xdg_data = _path_under(
        root, offline.get("xdg_data_home"), "offline_environment.xdg_data_home"
    )
    xdg_cache = _path_under(
        root, offline.get("xdg_cache_home"), "offline_environment.xdg_cache_home"
    )
    for label, path in (("XDG data", xdg_data), ("XDG cache", xdg_cache)):
        if path.is_symlink() or not path.is_dir():
            raise PortableUmuError(f"{label} directory is absent.")

    runtime = offline.get("runtime")
    if not isinstance(runtime, dict):
        raise PortableUmuError("Offline runtime declaration is absent.")
    family = runtime.get("family")
    if not isinstance(family, str) or not re.fullmatch(r"steamrt[0-9]+", family):
        raise PortableUmuError("Offline runtime family is invalid.")
    runtime_root = _path_under(
        xdg_data, runtime.get("path"), "offline_environment.runtime.path"
    )
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise PortableUmuError("Preserved Steam runtime directory is absent.")

    required = runtime.get("required_paths")
    if not isinstance(required, list) or not required:
        raise PortableUmuError("Offline runtime required_paths is absent.")
    for index, item in enumerate(required):
        if not isinstance(item, dict):
            raise PortableUmuError("Offline runtime required path is invalid.")
        path = _path_under(
            runtime_root,
            item.get("path"),
            f"offline_environment.runtime.required_paths[{index}]",
        )
        kind = item.get("type")
        if kind == "file":
            valid = path.is_file() and not path.is_symlink()
        elif kind == "directory":
            valid = path.is_dir() and not path.is_symlink()
        else:
            raise PortableUmuError("Offline runtime path type is invalid.")
        if not valid:
            raise PortableUmuError(
                f"Preserved Steam runtime component is absent: "
                f"{path.relative_to(runtime_root)}"
            )

    platform_prefix = _RUNTIME_PLATFORM_PREFIX.get(family)
    if platform_prefix is None:
        raise PortableUmuError(
            f"Unsupported preserved Steam runtime family: {family}."
        )
    platform_directories = [
        path
        for path in runtime_root.glob(f"{platform_prefix}_platform_*")
        if path.is_dir() and not path.is_symlink()
    ]
    if len(platform_directories) != 1:
        raise PortableUmuError(
            f"Expected exactly one complete "
            f"{platform_prefix}_platform_* directory for {family}."
        )
    if not (runtime_root / "VERSIONS.txt").is_file():
        raise PortableUmuError("Preserved runtime lacks VERSIONS.txt.")
    if not (runtime_root / "pressure-vessel").is_dir():
        raise PortableUmuError("Preserved runtime lacks pressure-vessel.")

    return {
        "xdg_data": xdg_data,
        "xdg_cache": xdg_cache,
        "runtime_root": runtime_root,
    }


def verify(root: Path) -> dict[str, Any]:
    canonical = _root_path(root)
    receipt = _receipt(canonical)
    runtime = _verify_offline_runtime(canonical, receipt)

    launchers = receipt.get("launchers")
    if not isinstance(launchers, list) or not launchers:
        raise PortableUmuError("UMU launcher receipts are absent.")
    for index, item in enumerate(launchers):
        if not isinstance(item, dict):
            raise PortableUmuError("UMU launcher receipt is invalid.")
        path = _path_under(
            canonical, item.get("path"), f"launchers[{index}].path"
        )
        expected = item.get("digest")
        if (
            path.is_symlink()
            or not path.is_file()
            or not isinstance(expected, str)
            or _sha256(path) != expected
        ):
            raise PortableUmuError(f"UMU launcher failed verification: {path}")
        if not os.access(path, os.X_OK):
            raise PortableUmuError(f"UMU launcher is not executable: {path}")

    paths = receipt.get("paths")
    if not isinstance(paths, dict):
        raise PortableUmuError("UMU receipt paths are absent.")
    runtime_var = _path_under(
        canonical, paths.get("runtime_var"), "paths.runtime_var"
    )
    if (
        runtime_var.is_symlink()
        or not runtime_var.is_dir()
        or any(runtime_var.iterdir())
    ):
        raise PortableUmuError("UMU mutable runtime directory is not empty.")

    operational = receipt.get("operational_paths")
    if not isinstance(operational, dict):
        raise PortableUmuError("UMU operational paths are absent.")
    for key in ("launcher", "verifier", "uninstaller", "portable_runtime"):
        path = _path_under(
            canonical, operational.get(key), f"operational_paths.{key}"
        )
        if path.is_symlink() or not path.is_file():
            raise PortableUmuError(f"UMU operational file is absent: {key}")
        if key != "portable_runtime" and not os.access(path, os.X_OK):
            raise PortableUmuError(
                f"UMU operational script is not executable: {key}"
            )

    inner_launcher = _path_under(
        canonical, paths.get("launcher"), "paths.launcher"
    )
    sanitizer = _path_under(
        canonical, paths.get("sanitizer"), "paths.sanitizer"
    )
    for label, path in (("launcher", inner_launcher), ("sanitizer", sanitizer)):
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise PortableUmuError(f"UMU {label} is absent or not executable.")

    return {
        "schema": 0,
        "capsule_id": receipt.get("capsule_id"),
        "profile_id": receipt.get("profile_id"),
        "backend": "umu",
        "destination": ".",
        "runtime_family": receipt["offline_environment"]["runtime"]["family"],
        "runtime_root": str(runtime["runtime_root"].relative_to(canonical)),
        "verified": True,
        "complete": True,
    }


def play(root: Path, *, arguments: Sequence[str] = ()) -> dict[str, Any]:
    canonical = _root_path(root)
    verification = verify(canonical)
    receipt = _receipt(canonical)
    paths = receipt["paths"]
    launcher = _path_under(canonical, paths.get("launcher"), "paths.launcher")
    sanitizer = _path_under(
        canonical, paths.get("sanitizer"), "paths.sanitizer"
    )
    runtime = _verify_offline_runtime(canonical, receipt)

    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise PortableUmuError(
            "systemd-run is required for mandatory network isolation."
        )

    environment = os.environ.copy()
    environment.update(
        {
            "XDG_DATA_HOME": str(runtime["xdg_data"]),
            "XDG_CACHE_HOME": str(runtime["xdg_cache"]),
            "UMU_RUNTIME_UPDATE": "0",
        }
    )
    command = [
        systemd_run,
        "--user",
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--property=PrivateNetwork=yes",
        "--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6",
        "--setenv=UMU_RUNTIME_UPDATE=0",
        f"--setenv=XDG_DATA_HOME={runtime['xdg_data']}",
        f"--setenv=XDG_CACHE_HOME={runtime['xdg_cache']}",
        "--",
        str(launcher),
        *arguments,
    ]

    started = time.monotonic_ns()
    process = subprocess.run(
        command,
        cwd=canonical,
        env=environment,
        check=False,
    )
    duration_ms = (time.monotonic_ns() - started) // 1_000_000
    sanitizer_process = subprocess.run(
        [str(sanitizer)],
        cwd=canonical,
        env=environment,
        check=False,
    )
    verified_after = False
    if sanitizer_process.returncode == 0:
        try:
            verify(canonical)
            verified_after = True
        except PortableUmuError:
            verified_after = False

    result = {
        "schema": 0,
        "capsule_id": verification["capsule_id"],
        "profile_id": verification["profile_id"],
        "backend": "umu",
        "destination": ".",
        "network": "isolated",
        "process_rc": int(process.returncode),
        "duration_ms": int(duration_ms),
        "sanitizer_rc": int(sanitizer_process.returncode),
        "verified_after_run": verified_after,
        "created_at": _now(),
        "complete": (
            process.returncode == 0
            and sanitizer_process.returncode == 0
            and verified_after
        ),
    }
    _write_json_atomic(canonical / LAST_RUN_RECEIPT, result)
    return result


def uninstall(
    root: Path,
    *,
    confirm_state_preserved: bool,
) -> dict[str, Any]:
    canonical = _root_path(root)
    verification = verify(canonical)
    receipt = _receipt(canonical)
    state_archives = receipt.get("state_archives", [])
    if not isinstance(state_archives, list):
        raise PortableUmuError("UMU state archive receipt is invalid.")
    if state_archives and not confirm_state_preserved:
        raise PortableUmuError(
            "Persistent state must be preserved before removal."
        )
    detached = canonical.parent / (
        f".ogv-remove-{canonical.name}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    if detached.exists() or detached.is_symlink():
        raise PortableUmuError("Removal staging path already exists.")
    os.rename(canonical, detached)
    try:
        shutil.rmtree(detached)
    except OSError as exc:
        if detached.exists() and not canonical.exists():
            try:
                os.rename(detached, canonical)
            except OSError:
                pass
        raise PortableUmuError("UMU tree was detached but not removed.") from exc
    return {
        "schema": 0,
        "capsule_id": verification["capsule_id"],
        "profile_id": verification["profile_id"],
        "backend": "umu",
        "destination": ".",
        "state_preservation_confirmed": confirm_state_preserved,
        "removed": True,
        "complete": True,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ogv-umu-runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--json", action="store_true")

    play_parser = commands.add_parser("play")
    play_parser.add_argument("--root", type=Path, required=True)
    play_parser.add_argument("--json", action="store_true")
    play_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    uninstall_parser = commands.add_parser("uninstall")
    uninstall_parser.add_argument("--root", type=Path, required=True)
    uninstall_parser.add_argument(
        "--confirm-state-preserved", action="store_true"
    )
    uninstall_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(args.root)
        elif args.command == "play":
            extra = list(args.arguments)
            if extra and extra[0] == "--":
                extra = extra[1:]
            result = play(args.root, arguments=extra)
        elif args.command == "uninstall":
            result = uninstall(
                args.root,
                confirm_state_preserved=args.confirm_state_preserved,
            )
        else:
            raise PortableUmuError("Unknown command.")
        if args.json:
            _print(result)
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        if args.command == "play":
            if result["sanitizer_rc"] != 0:
                return int(result["sanitizer_rc"])
            return int(result["process_rc"])
        return 0
    except PortableUmuError as exc:
        print(f"ogv-umu-runtime: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
