"""Self-contained runtime copied into playable materializations.

This module deliberately depends only on the Python standard library.  The
playable materializer copies it into each published materialization so the
generated launchers do not require an installed Offline Game Vault package.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Sequence

PLAYABLE_RECEIPT_NAME = "playable-materialization.json"
PLAY_RECEIPT_NAME = "last-play.json"
STATE_EXPORT_NAME = "state-export.json"


class PortableRuntimeError(Exception):
    """Raised when a portable play or uninstall operation is unsafe."""


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise PortableRuntimeError(f"{field} must be a non-empty string.")
    if "\x00" in value or "\\" in value:
        raise PortableRuntimeError(f"{field} is not a portable relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PortableRuntimeError(f"{field} is not a safe relative path.")
    return path


def _root_path(value: Path) -> Path:
    root = value.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise PortableRuntimeError(
            "Materialization root must be an existing regular directory."
        )
    resolved = root.resolve()
    if resolved == Path(resolved.anchor):
        raise PortableRuntimeError("Filesystem root is not a materialization.")
    if resolved == Path.home().resolve():
        raise PortableRuntimeError("Home directory is not a materialization.")
    return resolved


def _path_under(root: Path, value: Any, field: str) -> Path:
    relative = _safe_relative(value, field)
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PortableRuntimeError(f"{field} escapes the materialization.") from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableRuntimeError(f"{label} is absent or not a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PortableRuntimeError(f"{label} is not valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise PortableRuntimeError(f"{label} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise PortableRuntimeError(f"{label} must contain a JSON object.")
    return value


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.ogv-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    serialized = (
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            os.chmod(temporary, 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PortableRuntimeError("Protected path is not a regular file.")
        with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as handle:
            descriptor = None
            while True:
                block = handle.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise PortableRuntimeError("Cannot hash a regular file.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
        if getattr(before, field) != getattr(after, field):
            raise PortableRuntimeError("File changed while it was hashed.")
    return "sha256:" + digest.hexdigest(), total


def _receipt(root: Path) -> dict[str, Any]:
    document = _load_json(
        root / PLAYABLE_RECEIPT_NAME,
        "Playable materialization receipt",
    )
    if document.get("schema") != 0:
        raise PortableRuntimeError("Unsupported playable receipt schema.")
    if document.get("destination") != ".":
        raise PortableRuntimeError(
            "Playable receipt is not anchored to its own directory."
        )
    if document.get("backend") != "wine":
        raise PortableRuntimeError(
            "The portable runtime supports only the direct-Wine backend."
        )
    capsule_id = document.get("capsule_id")
    profile_id = document.get("profile_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        raise PortableRuntimeError("Playable receipt has no capsule_id.")
    if not isinstance(profile_id, str) or not profile_id:
        raise PortableRuntimeError("Playable receipt has no profile_id.")
    return document


def _required_paths(
    root: Path,
    receipt: dict[str, Any],
) -> dict[str, Path]:
    paths = receipt.get("paths")
    if not isinstance(paths, dict):
        raise PortableRuntimeError("Playable receipt has no paths object.")
    required_names = (
        "prefix",
        "runner",
        "wine",
        "wineserver",
        "entrypoint",
        "working_directory",
        "runtime",
        "launcher",
        "uninstaller",
        "portable_runtime",
    )
    resolved = {
        name: _path_under(root, paths.get(name), f"paths.{name}")
        for name in required_names
    }
    for name in ("prefix", "runner", "working_directory", "runtime"):
        path = resolved[name]
        if path.is_symlink() or not path.is_dir():
            raise PortableRuntimeError(f"paths.{name} is not a regular directory.")
    for name in (
        "wine",
        "wineserver",
        "entrypoint",
        "launcher",
        "uninstaller",
        "portable_runtime",
    ):
        path = resolved[name]
        if path.is_symlink() or not path.is_file():
            raise PortableRuntimeError(f"paths.{name} is not a regular file.")
    for name in ("wine", "wineserver", "launcher", "uninstaller"):
        if not os.access(resolved[name], os.X_OK):
            raise PortableRuntimeError(f"paths.{name} is not executable.")
    return resolved


def _verify_protected(
    root: Path,
    receipt: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    declarations = receipt.get("protected_files")
    if not isinstance(declarations, list) or not declarations:
        raise PortableRuntimeError(
            "Playable receipt has no protected-file declarations."
        )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise PortableRuntimeError("Protected-file declaration is invalid.")
        relative_value = declaration.get("path")
        relative = _safe_relative(
            relative_value,
            f"protected_files[{index}].path",
        )
        relative_text = relative.as_posix()
        if relative_text in seen:
            raise PortableRuntimeError("Duplicate protected-file path.")
        seen.add(relative_text)
        expected_digest = declaration.get("digest")
        expected_size = declaration.get("size")
        if (
            not isinstance(expected_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest)
            is None
        ):
            raise PortableRuntimeError("Protected-file digest is invalid.")
        if expected_size is not None and (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise PortableRuntimeError("Protected-file size is invalid.")
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise PortableRuntimeError(
                f"Protected file is absent: {relative_text}"
            )
        actual_digest, actual_size = _sha256_file(path)
        if actual_digest != expected_digest:
            raise PortableRuntimeError(
                f"Protected file failed verification: {relative_text}"
            )
        if expected_size is not None and actual_size != expected_size:
            raise PortableRuntimeError(
                f"Protected file has an unexpected size: {relative_text}"
            )
        results.append(
            {
                "path": relative_text,
                "digest": actual_digest,
                "size": actual_size,
                "verified": True,
            }
        )
    return tuple(results)


def verify_materialization(root: Path) -> dict[str, Any]:
    """Verify the portable receipt, paths, and protected files."""

    canonical = _root_path(root)
    receipt = _receipt(canonical)
    paths = _required_paths(canonical, receipt)
    protected = _verify_protected(canonical, receipt)
    prefix_operations = receipt.get("prefix_operations", [])
    if not isinstance(prefix_operations, list):
        raise PortableRuntimeError("prefix_operations must be an array.")
    for index, operation in enumerate(prefix_operations):
        if not isinstance(operation, dict):
            raise PortableRuntimeError("Prefix operation is invalid.")
        operation_type = operation.get("type")
        path = _path_under(
            canonical,
            operation.get("path"),
            f"prefix_operations[{index}].path",
        )
        if operation_type == "mkdir":
            if path.is_symlink() or not path.is_dir():
                raise PortableRuntimeError("Required prefix directory is absent.")
        elif operation_type == "symlink":
            target = operation.get("target")
            if not isinstance(target, str) or not target:
                raise PortableRuntimeError("Symlink target is invalid.")
            if not path.is_symlink():
                raise PortableRuntimeError("Required prefix symlink is absent.")
            if os.readlink(path) != target:
                raise PortableRuntimeError(
                    "Required prefix symlink has an unexpected target."
                )
            resolved = path.resolve(strict=False)
            try:
                resolved.relative_to(canonical)
            except ValueError as exc:
                raise PortableRuntimeError(
                    "Required prefix symlink escapes the materialization."
                ) from exc
        else:
            raise PortableRuntimeError("Unsupported prefix operation.")

    state = receipt.get("state")
    if not isinstance(state, dict):
        raise PortableRuntimeError("Playable receipt has no state object.")
    baseline, baseline_items = _baseline_state(root, receipt)
    declared_count = state.get("item_count")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count < 0
        or declared_count != len(baseline_items)
    ):
        raise PortableRuntimeError(
            "Playable state count does not match its verified baseline."
        )
    if declared_count > 0 and baseline.get("capsule_id") != receipt["capsule_id"]:
        raise PortableRuntimeError(
            "Baseline state belongs to another capsule."
        )

    return {
        "schema": 0,
        "capsule_id": receipt["capsule_id"],
        "profile_id": receipt["profile_id"],
        "backend": receipt["backend"],
        "protected_file_count": len(protected),
        "verified": True,
        "paths": {
            name: path.relative_to(canonical).as_posix()
            for name, path in paths.items()
        },
    }


def _runtime_environment(
    root: Path,
    receipt: dict[str, Any],
    paths: dict[str, Path],
) -> dict[str, str]:
    runtime = paths["runtime"]
    runtime_directories = {
        "HOME": runtime / "home",
        "TMPDIR": runtime / "tmp",
        "TMP": runtime / "tmp",
        "TEMP": runtime / "tmp",
        "XDG_CACHE_HOME": runtime / "cache",
        "XDG_CONFIG_HOME": runtime / "config",
        "XDG_DATA_HOME": runtime / "data",
    }
    for path in set(runtime_directories.values()):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise PortableRuntimeError(
                "Cannot create an isolated runtime directory."
            )

    launch = receipt.get("launch")
    if not isinstance(launch, dict):
        raise PortableRuntimeError("Playable receipt has no launch object.")
    network = launch.get("network", "host_default")
    if network == "isolated":
        raise PortableRuntimeError(
            "This direct-Wine runtime does not implement network isolation; "
            "refusing to claim an isolated launch."
        )
    if network not in {"allowed", "host_default"}:
        raise PortableRuntimeError("Unsupported launch network policy.")

    declared_environment = launch.get("environment", {})
    if not isinstance(declared_environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in declared_environment.items()
    ):
        raise PortableRuntimeError("Launch environment is invalid.")

    environment = os.environ.copy()
    environment.update(
        {name: str(path) for name, path in runtime_directories.items()}
    )
    environment["WINEPREFIX"] = str(paths["prefix"])
    environment["PATH"] = (
        str(paths["wine"].parent)
        + os.pathsep
        + "/usr/bin"
        + os.pathsep
        + "/bin"
    )
    environment.update(declared_environment)
    return environment


def play(
    root: Path,
    *,
    extra_arguments: Sequence[str] = (),
) -> dict[str, Any]:
    """Run a verified direct-Wine materialization and write a receipt."""

    canonical = _root_path(root)
    receipt = _receipt(canonical)
    verification_started_ns = time.monotonic_ns()
    verification = verify_materialization(canonical)
    paths = _required_paths(canonical, receipt)
    environment = _runtime_environment(canonical, receipt, paths)
    launch = receipt["launch"]
    arguments = launch.get("arguments", [])
    if not isinstance(arguments, list) or any(
        not isinstance(value, str) for value in arguments
    ):
        raise PortableRuntimeError("Launch arguments are invalid.")

    receipts_value = receipt.get("receipts_directory")
    receipts_dir = _path_under(
        canonical,
        receipts_value,
        "receipts_directory",
    )
    receipts_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if receipts_dir.is_symlink() or not receipts_dir.is_dir():
        raise PortableRuntimeError("Receipt directory is not safe.")

    prepared_at = _now()
    invoked_at = _now()
    invoked_ns = time.monotonic_ns()
    completed = subprocess.run(
        [
            str(paths["wine"]),
            str(paths["entrypoint"]),
            *arguments,
            *tuple(extra_arguments),
        ],
        cwd=paths["working_directory"],
        env=environment,
        check=False,
    )
    process_finished_at = _now()
    process_finished_ns = time.monotonic_ns()

    server_started_ns = time.monotonic_ns()
    server = subprocess.run(
        [str(paths["wineserver"]), "-w"],
        cwd=paths["working_directory"],
        env=environment,
        check=False,
    )
    finished_at = _now()
    finished_ns = time.monotonic_ns()

    document = {
        "schema": 0,
        "receipt_id": f"play-{secrets.token_hex(16)}",
        "capsule_id": receipt["capsule_id"],
        "profile_id": receipt["profile_id"],
        "backend": "wine",
        "prepared_at": prepared_at,
        "invoked_at": invoked_at,
        "process_finished_at": process_finished_at,
        "finished_at": finished_at,
        "preparation_ms": max(
            0,
            (invoked_ns - verification_started_ns) // 1_000_000,
        ),
        "process_duration_ms": max(
            0,
            (process_finished_ns - invoked_ns) // 1_000_000,
        ),
        "wineserver_wait_ms": max(
            0,
            (finished_ns - server_started_ns) // 1_000_000,
        ),
        "total_ms": max(
            0,
            (finished_ns - verification_started_ns) // 1_000_000,
        ),
        "game_process_rc": completed.returncode,
        "wineserver_wait_rc": server.returncode,
        "startup_window_ready_ms": None,
        "startup_window_ready_status": "not_instrumented",
        "protected_file_count": verification["protected_file_count"],
        "complete": completed.returncode == 0 and server.returncode == 0,
    }
    _write_json_atomic(receipts_dir / PLAY_RECEIPT_NAME, document)

    if server.returncode != 0:
        raise PortableRuntimeError(
            f"wineserver -w failed with return code {server.returncode}."
        )
    return document


def _file_record(path: str, metadata: os.stat_result, digest: str, size: int) -> dict[str, Any]:
    return {
        "path": path,
        "type": "file",
        "mode": stat.S_IMODE(metadata.st_mode),
        "bytes": size,
        "digest": digest,
    }


def _directory_record(path: str, metadata: os.stat_result) -> dict[str, Any]:
    return {
        "path": path,
        "type": "directory",
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _scan_state(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "present": False,
            "entry_type": "missing",
            "entries": [],
            "file_count": 0,
            "directory_count": 0,
            "bytes": 0,
        }

    if stat.S_ISLNK(metadata.st_mode):
        raise PortableRuntimeError("Persistent state contains a symlink.")
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise PortableRuntimeError(
                "Persistent-state file has multiple hard links."
            )
        digest, size = _sha256_file(path)
        return {
            "present": True,
            "entry_type": "file",
            "entries": [_file_record(".", metadata, digest, size)],
            "file_count": 1,
            "directory_count": 0,
            "bytes": size,
        }
    if not stat.S_ISDIR(metadata.st_mode):
        raise PortableRuntimeError("Persistent state is a special file.")

    entries: list[dict[str, Any]] = [_directory_record(".", metadata)]
    total_bytes = 0
    file_count = 0
    directory_count = 1

    def walk(directory: Path, relative: PurePosixPath) -> None:
        nonlocal total_bytes, file_count, directory_count
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name,
            )
        except OSError as exc:
            raise PortableRuntimeError(
                "Cannot enumerate persistent-state directory."
            ) from exc
        for child in children:
            child_path = directory / child.name
            child_relative = (
                PurePosixPath(child.name)
                if str(relative) == "."
                else relative / child.name
            )
            try:
                child_metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise PortableRuntimeError(
                    "Cannot inspect persistent-state entry."
                ) from exc
            if stat.S_ISLNK(child_metadata.st_mode):
                raise PortableRuntimeError(
                    "Persistent state contains a symlink."
                )
            if stat.S_ISDIR(child_metadata.st_mode):
                entries.append(
                    _directory_record(
                        child_relative.as_posix(),
                        child_metadata,
                    )
                )
                directory_count += 1
                walk(child_path, child_relative)
            elif stat.S_ISREG(child_metadata.st_mode):
                if child_metadata.st_nlink != 1:
                    raise PortableRuntimeError(
                        "Persistent-state file has multiple hard links."
                    )
                digest, size = _sha256_file(child_path)
                entries.append(
                    _file_record(
                        child_relative.as_posix(),
                        child_metadata,
                        digest,
                        size,
                    )
                )
                file_count += 1
                total_bytes += size
            else:
                raise PortableRuntimeError(
                    "Persistent state contains a special file."
                )

    walk(path, PurePosixPath("."))
    entries.sort(key=lambda item: (item["path"], item["type"]))
    return {
        "present": True,
        "entry_type": "directory",
        "entries": entries,
        "file_count": file_count,
        "directory_count": directory_count,
        "bytes": total_bytes,
    }


def _baseline_state(
    root: Path,
    receipt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    state = receipt.get("state")
    if not isinstance(state, dict):
        raise PortableRuntimeError("Playable receipt has no state object.")
    item_count = state.get("item_count")
    baseline_value = state.get("baseline_receipt")
    if item_count == 0 and baseline_value is None:
        return (
            {
                "schema": 0,
                "backup_id": None,
                "capsule_id": receipt["capsule_id"],
                "items": [],
            },
            {},
        )
    baseline_path = _path_under(
        root,
        baseline_value,
        "state.baseline_receipt",
    )
    expected_digest = state.get("baseline_receipt_digest")
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest)
        is None
    ):
        raise PortableRuntimeError(
            "Playable receipt has no valid baseline-state digest."
        )
    actual_digest, _ = _sha256_file(baseline_path)
    if actual_digest != expected_digest:
        raise PortableRuntimeError(
            "Baseline state receipt failed verification."
        )
    baseline = _load_json(baseline_path, "Baseline state receipt")
    if baseline.get("backup_id") != state.get("source_backup_id"):
        raise PortableRuntimeError(
            "Baseline state backup ID does not match the playable receipt."
        )
    items = baseline.get("items")
    if not isinstance(items, list):
        raise PortableRuntimeError("Baseline state receipt has no items array.")
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise PortableRuntimeError("Baseline state item is invalid.")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise PortableRuntimeError("Baseline state item has no ID.")
        if item_id in index:
            raise PortableRuntimeError("Duplicate baseline state item ID.")
        _safe_relative(
            item.get("declared_path"),
            f"baseline state {item_id}.declared_path",
        )
        entries = item.get("entries")
        if not isinstance(entries, list):
            raise PortableRuntimeError("Baseline state item has no entries.")
        index[item_id] = item
    return baseline, index


def state_status(root: Path) -> dict[str, Any]:
    canonical = _root_path(root)
    receipt = _receipt(canonical)
    paths = _required_paths(canonical, receipt)
    baseline, baseline_items = _baseline_state(canonical, receipt)
    current_items: list[dict[str, Any]] = []
    changed: list[str] = []

    for item_id, baseline_item in sorted(baseline_items.items()):
        relative = _safe_relative(
            baseline_item["declared_path"],
            f"state {item_id}.declared_path",
        )
        target = paths["prefix"].joinpath(*relative.parts)
        current = _scan_state(target)
        is_changed = (
            current["entry_type"] != baseline_item.get("entry_type")
            or current["entries"] != baseline_item.get("entries")
        )
        if is_changed:
            changed.append(item_id)
        current_items.append(
            {
                "id": item_id,
                "declared_path": relative.as_posix(),
                "kind": baseline_item.get("kind", "other"),
                "sensitive": bool(baseline_item.get("sensitive", False)),
                "required": bool(baseline_item.get("required", False)),
                "changed": is_changed,
                **current,
            }
        )
    return {
        "schema": 0,
        "capsule_id": receipt["capsule_id"],
        "profile_id": receipt["profile_id"],
        "baseline_backup_id": baseline.get("backup_id"),
        "item_count": len(current_items),
        "changed_count": len(changed),
        "changed_items": changed,
        "items": current_items,
    }


def _copy_state_payload(
    source: Path,
    destination: Path,
    current: dict[str, Any],
) -> None:
    entry_type = current["entry_type"]
    if entry_type == "missing":
        return
    if entry_type == "file":
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        return
    destination.mkdir(parents=True, mode=0o700)
    for record in current["entries"]:
        if record["path"] == ".":
            continue
        relative = _safe_relative(record["path"], "state entry path")
        target = destination.joinpath(*relative.parts)
        source_child = source.joinpath(*relative.parts)
        if record["type"] == "directory":
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(target, record["mode"])
        else:
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(source_child, target, follow_symlinks=False)
    for record in sorted(
        (
            item
            for item in current["entries"]
            if item["type"] == "directory"
        ),
        key=lambda item: len(PurePosixPath(item["path"]).parts),
        reverse=True,
    ):
        target = (
            destination
            if record["path"] == "."
            else destination.joinpath(
                *_safe_relative(record["path"], "state directory").parts
            )
        )
        os.chmod(target, record["mode"])


def _export_state(
    root: Path,
    export_root: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    receipt = _receipt(root)
    paths = _required_paths(root, receipt)
    export = export_root.expanduser().absolute()
    resolved_export = export.resolve(strict=False)
    if resolved_export == root or resolved_export.is_relative_to(root):
        raise PortableRuntimeError(
            "State export must be outside the materialization."
        )
    if export.exists():
        if export.is_symlink() or not export.is_dir() or any(export.iterdir()):
            raise PortableRuntimeError(
                "State-export destination must be absent or empty."
            )
        created_export_root = False
    else:
        export.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        export.mkdir(mode=0o700)
        created_export_root = True

    stage = export / (
        f".ogv-state-export-{os.getpid()}-{secrets.token_hex(8)}"
    )
    stage.mkdir(mode=0o700)
    output_items: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(status["items"]):
            relative = _safe_relative(
                item["declared_path"],
                f"state {item['id']}.declared_path",
            )
            source = paths["prefix"].joinpath(*relative.parts)
            payload_relative = (
                f"payload/{index:04d}-{item['id']}/data"
                if item["entry_type"] != "missing"
                else None
            )
            if payload_relative is not None:
                payload_path = stage.joinpath(
                    *_safe_relative(
                        payload_relative,
                        "export payload path",
                    ).parts
                )
                _copy_state_payload(source, payload_path, item)
                copied = _scan_state(payload_path)
                if (
                    copied["entry_type"] != item["entry_type"]
                    or copied["entries"] != item["entries"]
                ):
                    raise PortableRuntimeError(
                        "Exported state failed independent verification."
                    )
            output_items.append(
                {
                    "id": item["id"],
                    "declared_path": item["declared_path"],
                    "kind": item["kind"],
                    "sensitive": item["sensitive"],
                    "required": item["required"],
                    "present": item["present"],
                    "entry_type": item["entry_type"],
                    "payload_path": payload_relative,
                    "file_count": item["file_count"],
                    "directory_count": item["directory_count"],
                    "bytes": item["bytes"],
                    "entries": item["entries"],
                    "changed_from_baseline": item["changed"],
                }
            )

        document = {
            "schema": 0,
            "kind": "ogv-state-export",
            "export_id": f"state-export-{secrets.token_hex(16)}",
            "capsule_id": receipt["capsule_id"],
            "profile_id": receipt["profile_id"],
            "created_at": _now(),
            "baseline_backup_id": status["baseline_backup_id"],
            "item_count": len(output_items),
            "changed_count": status["changed_count"],
            "items": output_items,
            "verified": True,
            "complete": True,
        }
        _write_json_atomic(stage / STATE_EXPORT_NAME, document)

        for child in sorted(stage.iterdir(), key=lambda item: item.name):
            destination = export / child.name
            if destination.exists() or destination.is_symlink():
                raise PortableRuntimeError(
                    "Collision while publishing exported state."
                )
            os.replace(child, destination)
        stage.rmdir()
        return document
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if created_export_root:
            try:
                export.rmdir()
            except OSError:
                pass
        raise


def _active_wine_processes(prefix: Path) -> list[int]:
    if not sys.platform.startswith("linux"):
        raise PortableRuntimeError(
            "Automatic direct-Wine process detection is implemented only "
            "for Linux."
        )
    expected = ("WINEPREFIX=" + str(prefix)).encode()
    active: list[int] = []
    proc = Path("/proc")
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        try:
            values = (child / "environ").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if expected in values:
            active.append(int(child.name))
    return sorted(active)


def uninstall(
    root: Path,
    *,
    export_state: Path | None = None,
    discard_state: bool = False,
) -> dict[str, Any]:
    """Export or explicitly discard state, then remove a recognized tree."""

    if export_state is not None and discard_state:
        raise PortableRuntimeError(
            "--export-state and --discard-state are mutually exclusive."
        )
    canonical = _root_path(root)
    receipt = _receipt(canonical)
    verification = verify_materialization(canonical)
    paths = _required_paths(canonical, receipt)
    active = _active_wine_processes(paths["prefix"])
    if active:
        raise PortableRuntimeError(
            "Wine processes still use this materialization."
        )

    status = state_status(canonical)
    changed = status["changed_count"] > 0
    export_document: dict[str, Any] | None = None
    if changed and export_state is None and not discard_state:
        joined = ", ".join(status["changed_items"])
        raise PortableRuntimeError(
            "Persistent state changed; aborting removal. "
            f"Changed items: {joined}. Use --export-state or "
            "--discard-state."
        )
    if export_state is not None:
        export_document = _export_state(canonical, export_state, status)

    removal = receipt.get("removal")
    if not isinstance(removal, dict):
        raise PortableRuntimeError("Playable receipt has no removal object.")
    safe_to_remove = removal.get("safe_to_remove")
    if not isinstance(safe_to_remove, list):
        raise PortableRuntimeError("removal.safe_to_remove is invalid.")
    allowed: set[str] = set()
    for index, value in enumerate(safe_to_remove):
        relative = _safe_relative(
            value,
            f"removal.safe_to_remove[{index}]",
        )
        allowed.add(relative.parts[0])
    actual = {path.name for path in canonical.iterdir()}
    unknown = sorted(actual - allowed)
    if unknown:
        raise PortableRuntimeError(
            "Materialization contains unregistered top-level paths: "
            + ", ".join(unknown)
        )

    detached = canonical.parent / (
        f".ogv-remove-{canonical.name}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    if detached.exists() or detached.is_symlink():
        raise PortableRuntimeError("Removal staging path already exists.")
    os.rename(canonical, detached)
    try:
        shutil.rmtree(detached)
    except OSError as exc:
        raise PortableRuntimeError(
            "Materialization was detached but could not be fully removed."
        ) from exc

    return {
        "schema": 0,
        "capsule_id": receipt["capsule_id"],
        "profile_id": receipt["profile_id"],
        "backend": "wine",
        "verified_before_removal": verification["verified"],
        "changed_state_detected": changed,
        "changed_items": status["changed_items"],
        "state_exported": export_document is not None,
        "state_export_id": (
            export_document["export_id"]
            if export_document is not None
            else None
        ),
        "discard_state_authorized": discard_state,
        "removed": True,
        "complete": True,
    }


def _print_json(value: dict[str, Any]) -> None:
    print(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ogv-playable-runtime",
        description=(
            "Self-contained runtime for a published OGV direct-Wine "
            "materialization."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--json", action="store_true")

    play_parser = commands.add_parser("play")
    play_parser.add_argument("--root", type=Path, required=True)
    play_parser.add_argument("--json", action="store_true")
    play_parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        help="Additional game arguments after '--'.",
    )

    state_parser = commands.add_parser("state-status")
    state_parser.add_argument("--root", type=Path, required=True)
    state_parser.add_argument("--json", action="store_true")

    uninstall_parser = commands.add_parser("uninstall")
    uninstall_parser.add_argument("--root", type=Path, required=True)
    uninstall_parser.add_argument("--export-state", type=Path)
    uninstall_parser.add_argument("--discard-state", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_materialization(args.root)
        elif args.command == "play":
            extra = list(args.arguments)
            if extra and extra[0] == "--":
                extra = extra[1:]
            result = play(args.root, extra_arguments=extra)
        elif args.command == "state-status":
            result = state_status(args.root)
        elif args.command == "uninstall":
            result = uninstall(
                args.root,
                export_state=args.export_state,
                discard_state=args.discard_state,
            )
        else:
            raise PortableRuntimeError("Unknown portable runtime command.")
        if args.json:
            _print_json(result)
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        if args.command == "play":
            if result["wineserver_wait_rc"] != 0:
                return int(result["wineserver_wait_rc"])
            return int(result["game_process_rc"])
        return 0
    except PortableRuntimeError as exc:
        print(f"ogv-playable-runtime: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ogv-playable-runtime: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
