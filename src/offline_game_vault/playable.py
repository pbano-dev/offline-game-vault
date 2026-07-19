"""Capsule-driven playable materialization for direct Wine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import subprocess
from typing import Any, Sequence
import uuid

from . import __version__
from .materializer import (
    MaterializationError,
    _acquire_lock,
    _canonical_destination,
    _release_lock,
    _rename_noreplace,
    materialize_profile,
)
from .portable_runtime import (
    PLAYABLE_RECEIPT_NAME,
    PortableRuntimeError,
    play,
    uninstall,
    verify_materialization,
)
from .state_manager import (
    StateError,
    restore_state,
    verify_state_backup,
)

PORTABLE_RUNTIME_DESTINATION = "metadata/ogv_playable_runtime.py"
SOURCE_RECEIPT_DESTINATION = "metadata/source-materialization-receipt.json"
BASELINE_STATE_DESTINATION = "metadata/baseline-state.json"


class PlayableError(Exception):
    """Raised when a playable materialization cannot proceed safely."""


@dataclass(frozen=True)
class PlayableMaterializationResult:
    schema: int
    receipt_id: str
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    reused: bool
    object_count: int
    protected_file_count: int
    state_item_count: int
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlayableVerificationResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    protected_file_count: int
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlayResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    game_process_rc: int
    wineserver_wait_rc: int
    preparation_ms: int
    process_duration_ms: int
    wineserver_wait_ms: int
    total_ms: int
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlayableRemovalResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    changed_state_detected: bool
    changed_items: tuple[str, ...]
    state_exported: bool
    state_export_id: str | None
    discard_state_authorized: bool
    removed: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["changed_items"] = list(self.changed_items)
        return document


@dataclass(frozen=True)
class LayoutMapping:
    object_id: str
    source: PurePosixPath
    destination: PurePosixPath


@dataclass(frozen=True)
class PlayableContract:
    profile_status: str
    backend: str
    prefix: PurePosixPath
    runner: PurePosixPath
    wine: PurePosixPath
    wineserver: PurePosixPath
    runtime: PurePosixPath
    launcher: PurePosixPath
    uninstaller: PurePosixPath
    layout: tuple[LayoutMapping, ...]
    prefix_operations: tuple[dict[str, str], ...]
    protected_files: tuple[dict[str, Any], ...]
    launch: dict[str, Any]
    contract_digest: str


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise PlayableError(f"{field} must be a non-empty string.")
    if "\x00" in value or "\\" in value:
        raise PlayableError(f"{field} is not a portable relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PlayableError(f"{field} is not a safe relative path.")
    return path


def _path_under(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise PlayableError("A declared path escapes its materialization.") from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PlayableError(f"{label} is absent or not a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PlayableError(f"{label} is not valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise PlayableError(f"{label} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise PlayableError(f"{label} must contain a JSON object.")
    return value


def _canonical_json_digest(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _profile_and_contract(
    capsule_path: Path,
    profile_id: str,
) -> tuple[dict[str, Any], dict[str, Any], PlayableContract]:
    capsule = _load_json(capsule_path, "Capsule")
    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise PlayableError("capsule.profiles must be an array.")
    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if len(matches) != 1:
        raise PlayableError(
            "The requested profile must exist exactly once in the capsule."
        )
    profile = matches[0]
    if profile.get("adapter") != "wine":
        raise PlayableError(
            "Playable materialization currently supports adapter='wine' only."
        )
    if profile.get("platform") != "linux":
        raise PlayableError(
            "Direct-Wine playable materialization currently requires Linux."
        )
    status = profile.get("status")
    if status == "unavailable":
        raise PlayableError("The requested profile is unavailable.")
    if not isinstance(status, str):
        raise PlayableError("The requested profile has no valid status.")

    playable = profile.get("playable")
    if not isinstance(playable, dict):
        raise PlayableError(
            "The requested profile has no playable materialization contract."
        )
    if playable.get("schema") != 0:
        raise PlayableError("Unsupported playable contract schema.")
    if playable.get("backend") != "wine":
        raise PlayableError("Playable contract backend must be 'wine'.")

    paths = playable.get("paths")
    if not isinstance(paths, dict):
        raise PlayableError("playable.paths must be an object.")
    prefix = _safe_relative(paths.get("prefix"), "playable.paths.prefix")
    runner = _safe_relative(paths.get("runner"), "playable.paths.runner")
    wine = _safe_relative(paths.get("wine"), "playable.paths.wine")
    wineserver = _safe_relative(
        paths.get("wineserver"),
        "playable.paths.wineserver",
    )
    runtime = _safe_relative(paths.get("runtime"), "playable.paths.runtime")
    launcher = _safe_relative(paths.get("launcher"), "playable.paths.launcher")
    uninstaller = _safe_relative(
        paths.get("uninstaller"),
        "playable.paths.uninstaller",
    )

    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) or not value for value in dependencies
    ):
        raise PlayableError("profile.dependencies must be an array of IDs.")

    layout_value = playable.get("layout")
    if not isinstance(layout_value, list) or not layout_value:
        raise PlayableError("playable.layout must be a non-empty array.")
    layout: list[LayoutMapping] = []
    layout_objects: list[str] = []
    destinations: list[PurePosixPath] = []
    for index, item in enumerate(layout_value):
        if not isinstance(item, dict):
            raise PlayableError("Every playable.layout item must be an object.")
        object_id = item.get("object")
        if not isinstance(object_id, str) or not object_id:
            raise PlayableError("Every layout item requires an object ID.")
        source = _safe_relative(
            item.get("source"),
            f"playable.layout[{index}].source",
        )
        destination = _safe_relative(
            item.get("destination"),
            f"playable.layout[{index}].destination",
        )
        layout.append(
            LayoutMapping(
                object_id=object_id,
                source=source,
                destination=destination,
            )
        )
        layout_objects.append(object_id)
        destinations.append(destination)

    if len(layout_objects) != len(set(layout_objects)):
        raise PlayableError("A dependency may appear only once in playable.layout.")
    if set(layout_objects) != set(dependencies):
        raise PlayableError(
            "playable.layout must map every profile dependency exactly once."
        )
    for index, first in enumerate(destinations):
        for second in destinations[index + 1:]:
            if (
                first == second
                or first.is_relative_to(second)
                or second.is_relative_to(first)
            ):
                raise PlayableError("Playable layout destinations overlap.")

    prefix_operations_value = playable.get("prefix_operations", [])
    if not isinstance(prefix_operations_value, list):
        raise PlayableError("playable.prefix_operations must be an array.")
    prefix_operations: list[dict[str, str]] = []
    operation_paths: set[str] = set()
    for index, operation in enumerate(prefix_operations_value):
        if not isinstance(operation, dict):
            raise PlayableError("Prefix operation must be an object.")
        operation_type = operation.get("type")
        if operation_type not in {"mkdir", "symlink"}:
            raise PlayableError("Unsupported prefix operation type.")
        path = _safe_relative(
            operation.get("path"),
            f"playable.prefix_operations[{index}].path",
        )
        if not path.is_relative_to(prefix):
            raise PlayableError("Prefix operation must stay below the prefix.")
        if path.as_posix() in operation_paths:
            raise PlayableError("Duplicate prefix operation path.")
        operation_paths.add(path.as_posix())
        normalized = {
            "type": operation_type,
            "path": path.as_posix(),
        }
        if operation_type == "symlink":
            target = operation.get("target")
            if not isinstance(target, str) or not target:
                raise PlayableError("Symlink prefix operation needs a target.")
            if PurePosixPath(target).is_absolute():
                raise PlayableError("Prefix symlink target must be relative.")
            normalized["target"] = target
        prefix_operations.append(normalized)

    protected_value = playable.get("protected_files")
    if not isinstance(protected_value, list) or not protected_value:
        raise PlayableError("playable.protected_files must be non-empty.")
    protected_files: list[dict[str, Any]] = []
    protected_paths: set[str] = set()
    for index, item in enumerate(protected_value):
        if not isinstance(item, dict):
            raise PlayableError("Protected-file declaration must be an object.")
        path = _safe_relative(
            item.get("path"),
            f"playable.protected_files[{index}].path",
        )
        digest = item.get("digest")
        size = item.get("size")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
        ):
            raise PlayableError("Protected-file digest is invalid.")
        if size is not None and (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise PlayableError("Protected-file size is invalid.")
        if path.as_posix() in protected_paths:
            raise PlayableError("Duplicate protected-file path.")
        protected_paths.add(path.as_posix())
        declaration: dict[str, Any] = {
            "path": path.as_posix(),
            "digest": digest,
        }
        if size is not None:
            declaration["size"] = size
        protected_files.append(declaration)

    launch = profile.get("launch")
    if not isinstance(launch, dict):
        raise PlayableError("profile.launch must be an object.")
    entrypoint = _safe_relative(
        launch.get("entrypoint"),
        "profile.launch.entrypoint",
    )
    working_value = launch.get("working_directory")
    working_directory = (
        _safe_relative(
            working_value,
            "profile.launch.working_directory",
        )
        if working_value is not None
        else entrypoint.parent
    )
    arguments = launch.get("arguments", [])
    environment = launch.get("environment", {})
    network = launch.get("network", "host_default")
    if not isinstance(arguments, list) or any(
        not isinstance(value, str) for value in arguments
    ):
        raise PlayableError("profile.launch.arguments is invalid.")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise PlayableError("profile.launch.environment is invalid.")
    if network not in {"allowed", "host_default"}:
        raise PlayableError(
            "Direct Wine does not yet implement an isolated network launch; "
            "use host_default or allowed and document the limitation."
        )

    contract = PlayableContract(
        profile_status=status,
        backend="wine",
        prefix=prefix,
        runner=runner,
        wine=wine,
        wineserver=wineserver,
        runtime=runtime,
        launcher=launcher,
        uninstaller=uninstaller,
        layout=tuple(layout),
        prefix_operations=tuple(prefix_operations),
        protected_files=tuple(protected_files),
        launch={
            "entrypoint": entrypoint.as_posix(),
            "working_directory": working_directory.as_posix(),
            "arguments": list(arguments),
            "environment": dict(environment),
            "network": network,
        },
        contract_digest=_canonical_json_digest(
            {
                "adapter": profile.get("adapter"),
                "platform": profile.get("platform"),
                "status": status,
                "dependencies": list(dependencies),
                "launch": {
                    "entrypoint": entrypoint.as_posix(),
                    "working_directory": working_directory.as_posix(),
                    "arguments": list(arguments),
                    "environment": dict(environment),
                    "network": network,
                },
                "playable": playable,
            }
        ),
    )
    return capsule, profile, contract


def _filesystem_probe(stage: Path) -> None:
    probe = stage / ".ogv-filesystem-probe"
    probe.mkdir(mode=0o700)
    upper = probe / "Aa"
    lower = probe / "aa"
    upper.write_text("A", encoding="utf-8")
    lower.write_text("a", encoding="utf-8")
    if upper.read_text(encoding="utf-8") == lower.read_text(encoding="utf-8"):
        raise PlayableError(
            "Destination filesystem is not case-sensitive."
        )
    executable = probe / "executable.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(executable, 0o700)
    if not os.access(executable, os.X_OK):
        raise PlayableError("Destination does not preserve executable bits.")
    link = probe / "c:"
    os.symlink("../.ogv-filesystem-probe", link)
    if not link.is_symlink():
        raise PlayableError("Destination does not support required symlinks.")
    shutil.rmtree(probe)


def _write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.ogv-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            os.chmod(temporary, mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _runtime_source() -> str:
    from . import portable_runtime

    path = Path(portable_runtime.__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise PlayableError("Cannot locate the portable runtime source.")
    return path.read_text(encoding="utf-8")


def _launcher_text() -> str:
    return """#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" \\
  "$root/metadata/ogv_playable_runtime.py" \\
  play --root "$root" -- "$@"
"""


def _uninstaller_text() -> str:
    return """#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" \\
  "$root/metadata/ogv_playable_runtime.py" \\
  uninstall --root "$root" "$@"
"""


def _validate_generated_shell(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise PlayableError(
            f"Generated launcher failed bash -n: {path.name}"
        )


def _move_layout(
    *,
    raw: Path,
    stage: Path,
    contract: PlayableContract,
) -> None:
    objects_root = raw / "objects"
    for mapping in contract.layout:
        object_root = objects_root / mapping.object_id
        source = object_root.joinpath(*mapping.source.parts)
        destination = stage.joinpath(*mapping.destination.parts)
        if source.is_symlink() or not source.exists():
            raise PlayableError(
                f"Declared layout source is absent: {mapping.object_id}/"
                f"{mapping.source.as_posix()}"
            )
        if destination.exists() or destination.is_symlink():
            raise PlayableError("Playable layout destination already exists.")
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.replace(source, destination)
        remaining = list(object_root.iterdir())
        if remaining:
            raise PlayableError(
                "Layout mapping would leave undeclared object content: "
                f"{mapping.object_id}"
            )
        object_root.rmdir()
    if objects_root.exists():
        if any(objects_root.iterdir()):
            raise PlayableError("Unmapped object directories remain.")
        objects_root.rmdir()


def _apply_prefix_operations(
    stage: Path,
    operations: tuple[dict[str, str], ...],
) -> None:
    for operation in operations:
        path = _path_under(
            stage,
            _safe_relative(operation["path"], "prefix operation path"),
        )
        if path.exists() or path.is_symlink():
            raise PlayableError(
                "Prefix operation would replace an existing path."
            )
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if operation["type"] == "mkdir":
            path.mkdir(mode=0o700)
        else:
            target = operation["target"]
            os.symlink(target, path)
            resolved = path.resolve(strict=False)
            try:
                resolved.relative_to(stage.resolve())
            except ValueError as exc:
                raise PlayableError(
                    "Prefix symlink escapes the materialization."
                ) from exc


def _verify_runner_symlinks(runner: Path) -> int:
    count = 0
    for path in runner.rglob("*"):
        if not path.is_symlink():
            continue
        count += 1
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(runner.resolve())
        except ValueError as exc:
            raise PlayableError("Runner symlink escapes the runner root.") from exc
        if not path.exists():
            raise PlayableError("Runner contains a broken symlink.")
    return count


def _augment_runtime_protected(
    stage: Path,
    contract: PlayableContract,
) -> tuple[dict[str, Any], ...]:
    declarations = [dict(item) for item in contract.protected_files]
    known = {item["path"] for item in declarations}
    for relative in (contract.wine, contract.wineserver):
        relative_text = relative.as_posix()
        if relative_text in known:
            continue
        path = _path_under(stage, relative)
        if path.is_symlink() or not path.is_file():
            raise PlayableError(
                f"Runner executable is absent: {relative_text}"
            )
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while True:
                block = handle.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
        declarations.append(
            {
                "path": relative_text,
                "digest": "sha256:" + digest.hexdigest(),
                "size": total,
            }
        )
        known.add(relative_text)
    return tuple(declarations)


def _verify_protected(
    stage: Path,
    declarations: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    results: list[dict[str, Any]] = []
    for declaration in declarations:
        relative = _safe_relative(declaration["path"], "protected file path")
        path = stage.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise PlayableError(
                f"Protected file is absent: {relative.as_posix()}"
            )
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while True:
                block = handle.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
        actual = "sha256:" + digest.hexdigest()
        if actual != declaration["digest"]:
            raise PlayableError(
                f"Protected file failed verification: {relative.as_posix()}"
            )
        expected_size = declaration.get("size")
        if expected_size is not None and total != expected_size:
            raise PlayableError(
                f"Protected file has an unexpected size: {relative.as_posix()}"
            )
        results.append(
            {
                "path": relative.as_posix(),
                "digest": actual,
                "size": total,
                "verified": True,
            }
        )
    return tuple(results)


def _state_declarations(capsule: dict[str, Any]) -> list[dict[str, Any]]:
    value = capsule.get("persistent_state", [])
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise PlayableError("capsule.persistent_state must be an array.")
    return [
        item
        for item in value
        if item.get("backup", True) is True
    ]


def _existing_result(
    *,
    destination: Path,
    capsule_id: str,
    profile_id: str,
    contract: PlayableContract,
) -> PlayableMaterializationResult:
    try:
        verification = verify_materialization(destination)
    except PortableRuntimeError as exc:
        raise PlayableError(
            "Destination exists but is not a valid playable materialization."
        ) from exc
    receipt = _load_json(
        destination / PLAYABLE_RECEIPT_NAME,
        "Playable materialization receipt",
    )
    if receipt.get("capsule_id") != capsule_id:
        raise PlayableError("Existing materialization belongs to another capsule.")
    if receipt.get("profile_id") != profile_id:
        raise PlayableError("Existing materialization uses another profile.")
    if receipt.get("contract_digest") != contract.contract_digest:
        raise PlayableError("Existing materialization uses another contract.")
    objects = receipt.get("objects")
    state = receipt.get("state")
    if not isinstance(objects, list) or not isinstance(state, dict):
        raise PlayableError("Existing playable receipt is incomplete.")
    return PlayableMaterializationResult(
        schema=0,
        receipt_id=str(receipt["receipt_id"]),
        capsule_id=capsule_id,
        profile_id=profile_id,
        backend="wine",
        destination=str(destination),
        reused=True,
        object_count=len(objects),
        protected_file_count=verification["protected_file_count"],
        state_item_count=int(state.get("item_count", 0)),
        complete=True,
    )


def materialize_playable_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
    destination: Path,
    state_backup: Path | None = None,
) -> PlayableMaterializationResult:
    """Build, restore, verify, and atomically publish a playable profile."""

    capsule_path = capsule_path.expanduser().absolute()
    vault_root = vault_root.expanduser().resolve()
    destination = _canonical_destination(destination)
    capsule, profile, contract = _profile_and_contract(
        capsule_path,
        profile_id,
    )
    capsule_id = capsule.get("capsule_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        raise PlayableError("Capsule has no valid capsule_id.")

    try:
        destination.relative_to(vault_root)
    except ValueError:
        pass
    else:
        raise PlayableError("Destination must be outside the immutable vault.")

    if destination.exists() or destination.is_symlink():
        return _existing_result(
            destination=destination,
            capsule_id=capsule_id,
            profile_id=profile_id,
            contract=contract,
        )

    state_declarations = _state_declarations(capsule)
    state_verification = None
    if state_declarations:
        if state_backup is None:
            raise PlayableError(
                "This capsule declares persistent state; --state-backup "
                "is required."
            )
        try:
            state_verification = verify_state_backup(
                capsule_path=capsule_path,
                backup=state_backup,
            )
        except StateError as exc:
            raise PlayableError(str(exc)) from exc
        if not state_verification.verified:
            raise PlayableError("State backup failed verification.")
    elif state_backup is not None:
        raise PlayableError(
            "A state backup was supplied but the capsule declares no state."
        )

    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock, lock_descriptor = _acquire_lock(destination)
    token = f"{os.getpid()}-{secrets.token_hex(8)}"
    raw = destination.parent / f".ogv-playable-raw-{destination.name}-{token}"
    stage = destination.parent / f".ogv-playable-stage-{destination.name}-{token}"
    snapshot = destination.parent / (
        f".ogv-playable-snapshot-{destination.name}-{token}"
    )
    promoted = False
    raw_created = False

    try:
        if destination.exists() or destination.is_symlink():
            raise PlayableError("Destination appeared after locking.")
        stage.mkdir(mode=0o700)
        _filesystem_probe(stage)

        raw_result = materialize_profile(
            capsule_path=capsule_path,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=raw,
        )
        raw_created = True
        raw_receipt = _load_json(
            raw / "materialization-receipt.json",
            "Source materialization receipt",
        )
        _move_layout(raw=raw, stage=stage, contract=contract)
        metadata = stage / "metadata"
        metadata.mkdir(mode=0o700)
        source_receipt_path = stage / SOURCE_RECEIPT_DESTINATION
        _write_atomic(
            source_receipt_path,
            json.dumps(
                raw_receipt,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            0o600,
        )
        (raw / "materialization-receipt.json").unlink()
        raw.rmdir()
        raw_created = False

        _apply_prefix_operations(stage, contract.prefix_operations)
        runner_path = _path_under(stage, contract.runner)
        if runner_path.is_symlink() or not runner_path.is_dir():
            raise PlayableError("Declared runner root is not a directory.")
        runner_symlink_count = _verify_runner_symlinks(runner_path)

        receipts = stage / "receipts"
        receipts.mkdir(mode=0o700)
        state_restore_receipt: str | None = None
        baseline_state_digest: str | None = None
        baseline_backup_id: str | None = None
        if state_declarations:
            assert state_backup is not None
            prefix_path = _path_under(stage, contract.prefix)
            try:
                restore_result = restore_state(
                    capsule_path=capsule_path,
                    state_root=prefix_path,
                    backup=state_backup,
                    snapshot=snapshot,
                    confirm_stopped=True,
                )
            except StateError as exc:
                raise PlayableError(str(exc)) from exc
            snapshot_destination = receipts / "pre-restore-snapshot"
            os.replace(snapshot, snapshot_destination)
            state_restore_receipt = (
                "receipts/pre-restore-snapshot/state-restore-receipt.json"
            )
            baseline_source = state_backup / "state-backup.json"
            baseline_destination = stage / BASELINE_STATE_DESTINATION
            shutil.copy2(
                baseline_source,
                baseline_destination,
                follow_symlinks=False,
            )
            baseline_state_digest = (
                "sha256:"
                + hashlib.sha256(
                    baseline_destination.read_bytes()
                ).hexdigest()
            )
            baseline_document = _load_json(
                baseline_destination,
                "Baseline state receipt",
            )
            baseline_backup_id = baseline_document.get("backup_id")
            if not isinstance(baseline_backup_id, str):
                raise PlayableError(
                    "Baseline state receipt has no backup_id."
                )
            if restore_result.restored_count != len(state_declarations):
                raise PlayableError(
                    "Not every declared state item was restored."
                )

        protected_declarations = _augment_runtime_protected(
            stage,
            contract,
        )
        protected = _verify_protected(stage, protected_declarations)
        runtime_source = _runtime_source()
        runtime_path = stage / PORTABLE_RUNTIME_DESTINATION
        _write_atomic(runtime_path, runtime_source, 0o600)
        compile(runtime_source, str(runtime_path), "exec")

        runtime_root = _path_under(stage, contract.runtime)
        runtime_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        for relative_name in ("home", "tmp", "cache", "config", "data"):
            (runtime_root / relative_name).mkdir(mode=0o700, exist_ok=True)

        launcher_path = _path_under(stage, contract.launcher)
        uninstaller_path = _path_under(stage, contract.uninstaller)
        _write_atomic(launcher_path, _launcher_text(), 0o700)
        _write_atomic(uninstaller_path, _uninstaller_text(), 0o700)
        _validate_generated_shell(launcher_path)
        _validate_generated_shell(uninstaller_path)

        for required_name, relative in {
            "prefix": contract.prefix,
            "runner": contract.runner,
            "wine": contract.wine,
            "wineserver": contract.wineserver,
            "runtime": contract.runtime,
        }.items():
            path = _path_under(stage, relative)
            if required_name in {"prefix", "runner", "runtime"}:
                if path.is_symlink() or not path.is_dir():
                    raise PlayableError(
                        f"Declared {required_name} path is not a directory."
                    )
            else:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not os.access(path, os.X_OK)
                ):
                    raise PlayableError(
                        f"Declared {required_name} is not executable."
                    )

        entrypoint = _path_under(
            stage,
            _safe_relative(
                contract.launch["entrypoint"],
                "launch.entrypoint",
            ),
        )
        working_directory = _path_under(
            stage,
            _safe_relative(
                contract.launch["working_directory"],
                "launch.working_directory",
            ),
        )
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise PlayableError("Declared entrypoint is absent.")
        if working_directory.is_symlink() or not working_directory.is_dir():
            raise PlayableError("Declared working directory is absent.")

        receipt_id = f"playable-materialization-{uuid.uuid4()}"
        anticipated_top_level = {
            path.parts[0]
            for path in (
                contract.prefix,
                contract.runner,
                contract.runtime,
                contract.launcher,
                contract.uninstaller,
                PurePosixPath("metadata"),
                PurePosixPath("receipts"),
                PurePosixPath(PLAYABLE_RECEIPT_NAME),
            )
        }
        receipt = {
            "schema": 0,
            "receipt_id": receipt_id,
            "capsule_id": capsule_id,
            "profile_id": profile_id,
            "profile_status": contract.profile_status,
            "backend": "wine",
            "created_at": _now(),
            "orchestrator_version": __version__,
            "destination": ".",
            "contract_digest": contract.contract_digest,
            "objects": raw_receipt["objects"],
            "layout": [
                {
                    "object": item.object_id,
                    "source": item.source.as_posix(),
                    "destination": item.destination.as_posix(),
                }
                for item in contract.layout
            ],
            "paths": {
                "prefix": contract.prefix.as_posix(),
                "runner": contract.runner.as_posix(),
                "wine": contract.wine.as_posix(),
                "wineserver": contract.wineserver.as_posix(),
                "entrypoint": contract.launch["entrypoint"],
                "working_directory": contract.launch["working_directory"],
                "runtime": contract.runtime.as_posix(),
                "launcher": contract.launcher.as_posix(),
                "uninstaller": contract.uninstaller.as_posix(),
                "portable_runtime": PORTABLE_RUNTIME_DESTINATION,
            },
            "prefix_operations": list(contract.prefix_operations),
            "runner_symlink_count": runner_symlink_count,
            "protected_files": list(protected),
            "launch": contract.launch,
            "receipts_directory": "receipts",
            "state": {
                "item_count": len(state_declarations),
                "restored": bool(state_declarations),
                "source_backup_id": baseline_backup_id,
                "baseline_receipt": (
                    BASELINE_STATE_DESTINATION
                    if state_declarations
                    else None
                ),
                "baseline_receipt_digest": baseline_state_digest,
                "restore_receipt": state_restore_receipt,
            },
            "removal": {
                "safe_to_remove": sorted(anticipated_top_level),
                "must_preserve": [
                    item["path"]
                    for item in state_declarations
                    if item.get("backup", True) is True
                ],
            },
            "limitations": [
                "Network isolation is not implemented by the direct-Wine "
                "runtime.",
                "Window-ready startup latency is not instrumented.",
                "Host display, audio, and runtime sockets may exist outside "
                "the materialization.",
            ],
            "complete": True,
        }
        receipt_path = stage / PLAYABLE_RECEIPT_NAME
        _write_atomic(
            receipt_path,
            json.dumps(
                receipt,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            0o600,
        )

        verification = verify_materialization(stage)
        _rename_noreplace(stage, destination)
        promoted = True
        return PlayableMaterializationResult(
            schema=0,
            receipt_id=receipt_id,
            capsule_id=capsule_id,
            profile_id=profile_id,
            backend="wine",
            destination=str(destination),
            reused=False,
            object_count=raw_result.object_count,
            protected_file_count=verification["protected_file_count"],
            state_item_count=len(state_declarations),
            complete=True,
        )
    except (
        MaterializationError,
        PortableRuntimeError,
        StateError,
        OSError,
    ) as exc:
        raise PlayableError(str(exc)) from exc
    finally:
        if raw_created and raw.exists():
            shutil.rmtree(raw, ignore_errors=True)
        if not promoted and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if snapshot.exists():
            shutil.rmtree(snapshot, ignore_errors=True)
        _release_lock(lock, lock_descriptor)


def verify_playable_profile(
    *,
    destination: Path,
) -> PlayableVerificationResult:
    try:
        result = verify_materialization(destination)
    except PortableRuntimeError as exc:
        raise PlayableError(str(exc)) from exc
    return PlayableVerificationResult(
        schema=0,
        capsule_id=result["capsule_id"],
        profile_id=result["profile_id"],
        backend=result["backend"],
        destination=str(destination.expanduser().resolve()),
        protected_file_count=result["protected_file_count"],
        verified=True,
    )


def run_playable_profile(
    *,
    destination: Path,
    arguments: Sequence[str] = (),
) -> PlayResult:
    try:
        result = play(
            destination,
            extra_arguments=arguments,
        )
    except PortableRuntimeError as exc:
        raise PlayableError(str(exc)) from exc
    return PlayResult(
        schema=0,
        capsule_id=result["capsule_id"],
        profile_id=result["profile_id"],
        backend=result["backend"],
        destination=str(destination.expanduser().resolve()),
        game_process_rc=result["game_process_rc"],
        wineserver_wait_rc=result["wineserver_wait_rc"],
        preparation_ms=result["preparation_ms"],
        process_duration_ms=result["process_duration_ms"],
        wineserver_wait_ms=result["wineserver_wait_ms"],
        total_ms=result["total_ms"],
        complete=result["complete"],
    )


def remove_playable_profile(
    *,
    destination: Path,
    export_state: Path | None = None,
    discard_state: bool = False,
) -> PlayableRemovalResult:
    canonical = destination.expanduser().resolve()
    try:
        result = uninstall(
            canonical,
            export_state=export_state,
            discard_state=discard_state,
        )
    except PortableRuntimeError as exc:
        raise PlayableError(str(exc)) from exc
    return PlayableRemovalResult(
        schema=0,
        capsule_id=result["capsule_id"],
        profile_id=result["profile_id"],
        backend=result["backend"],
        destination=str(canonical),
        changed_state_detected=result["changed_state_detected"],
        changed_items=tuple(result["changed_items"]),
        state_exported=result["state_exported"],
        state_export_id=result["state_export_id"],
        discard_state_authorized=result["discard_state_authorized"],
        removed=result["removed"],
        complete=result["complete"],
    )
