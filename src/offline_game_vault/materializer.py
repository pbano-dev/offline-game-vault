"""Verified staging, atomic materialization, and safe removal."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ctypes
import errno
import json
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import shutil
import sys
from typing import Any
import uuid

from . import __version__
from .planner import PlanError, build_plan
from .profile_store import (
    ProfileStoreError,
    load_profile_definition,
    verify_profile,
)
from .safe_tar import (
    SafeTarError,
    TarExtractionResult,
    extract_tar_safely,
)
from .verifier import (
    ObjectSpec,
    VerifyError,
    resolve_capsule_object,
    verify_object,
)


RECEIPT_NAME = "materialization-receipt.json"


class MaterializationError(Exception):
    """Raised when materialization or removal cannot proceed safely."""


@dataclass(frozen=True)
class MaterializedObject:
    object_id: str
    digest: str
    destination: str
    strategy: str
    verified: bool
    member_count: int | None
    regular_bytes: int | None
    symlink_count: int | None
    hardlink_count: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MaterializationResult:
    schema: int
    receipt_id: str
    capsule_id: str
    profile_id: str
    destination: str
    object_count: int
    complete: bool
    objects: tuple[MaterializedObject, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objects"] = [asdict(item) for item in self.objects]
        return data


@dataclass(frozen=True)
class RemovalResult:
    schema: int
    capsule_id: str
    profile_id: str
    destination: str
    removed: bool
    persistent_state_declared: int
    state_preservation_confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_relative_path(value: str, field: str) -> PurePosixPath:
    if "\x00" in value or "\\" in value:
        raise MaterializationError(
            f"{field} is not a safe portable relative path: {value!r}"
        )

    path = PurePosixPath(value)
    if path.is_absolute():
        raise MaterializationError(
            f"{field} must be relative: {value!r}"
        )
    if any(part == ".." for part in path.parts):
        raise MaterializationError(
            f"{field} contains path traversal: {value!r}"
        )
    if not path.parts:
        raise MaterializationError(
            f"{field} must not be empty"
        )
    return path


def _canonical_destination(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    parent = expanded.parent.resolve()
    destination = parent / expanded.name

    if destination == Path(destination.anchor):
        raise MaterializationError(
            f"Refusing filesystem root as destination: {destination}"
        )
    if destination == Path.home().resolve():
        raise MaterializationError(
            "Refusing the home directory as a materialization destination."
        )
    return destination


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an existing destination."""

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise MaterializationError(
                "Linux libc does not expose renameat2; "
                "atomic no-replace promotion is unavailable."
            )

        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int

        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise MaterializationError(
                    f"Destination already exists: {destination}"
                )
            raise MaterializationError(
                "Atomic promotion failed: "
                f"{os.strerror(error_number)}"
            )
        return

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise MaterializationError(
                f"Destination already exists: {destination}"
            ) from exc
        except OSError as exc:
            raise MaterializationError(
                f"Atomic promotion failed: {exc}"
            ) from exc
        return

    raise MaterializationError(
        "Atomic no-replace directory promotion is currently "
        "implemented only for Linux and Windows."
    )


def _acquire_lock(destination: Path) -> tuple[Path, int]:
    lock = destination.parent / f".ogv-lock-{destination.name}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL

    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise MaterializationError(
            f"Another operation holds the destination lock: {lock}"
        ) from exc
    except OSError as exc:
        raise MaterializationError(
            f"Cannot create destination lock {lock}: {exc}"
        ) from exc

    return lock, descriptor


def _release_lock(lock: Path, descriptor: int) -> None:
    try:
        os.close(descriptor)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _load_capsule(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MaterializationError(
            f"Capsule not found: {path}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MaterializationError(
            f"Capsule is not valid UTF-8: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MaterializationError(
            f"Invalid JSON in {path}:{exc.lineno}:{exc.colno}: "
            f"{exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise MaterializationError(
            "Capsule top-level value must be an object."
        )
    return data


def _profile_and_state(
    *,
    capsule: dict[str, Any],
    profile_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise MaterializationError("capsule.profiles must be an array")

    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if not matches:
        raise MaterializationError(f"Unknown profile: {profile_id!r}")
    if len(matches) > 1:
        raise MaterializationError(
            f"Duplicate profile ID: {profile_id!r}"
        )

    state = capsule.get("persistent_state", [])
    if not isinstance(state, list):
        raise MaterializationError(
            "capsule.persistent_state must be an array"
        )
    if any(not isinstance(item, dict) for item in state):
        raise MaterializationError(
            "Every persistent_state entry must be an object"
        )

    return matches[0], state


def _copy_regular_object(
    *,
    source_spec: ObjectSpec,
    destination: Path,
    chunk_size: int = 8 * 1024 * 1024,
) -> int:
    source_result = verify_object(source_spec)
    if not source_result.verified:
        raise MaterializationError(
            f"Source object does not verify: {source_spec.object_id}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise MaterializationError(
            f"Copy destination already exists: {destination}"
        )

    written = 0
    try:
        with source_spec.path.open("rb", buffering=0) as source:
            before = os.fstat(source.fileno())

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW

            descriptor = os.open(destination, flags, 0o600)
            try:
                with os.fdopen(
                    descriptor,
                    "wb",
                    buffering=0,
                    closefd=True,
                ) as target:
                    descriptor = -1

                    while True:
                        block = source.read(chunk_size)
                        if not block:
                            break
                        target.write(block)
                        written += len(block)

                    target.flush()
                    os.fsync(target.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            after = os.fstat(source.fileno())
    except OSError as exc:
        raise MaterializationError(
            f"Cannot copy object {source_spec.object_id}: {exc}"
        ) from exc

    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
        if getattr(before, field, None) != getattr(after, field, None):
            raise MaterializationError(
                "Source object changed while copying: "
                f"{source_spec.object_id}"
            )

    if written != source_result.actual_size:
        raise MaterializationError(
            "Copied byte count does not match verified source size: "
            f"{source_spec.object_id}"
        )

    destination_result = verify_object(
        ObjectSpec(
            object_id=source_spec.object_id,
            path=destination,
            expected_digest=source_spec.expected_digest,
            expected_size=source_spec.expected_size,
            vault_root=None,
        )
    )
    if not destination_result.verified:
        raise MaterializationError(
            f"Copied destination does not verify: {source_spec.object_id}"
        )

    return written


def _materialize_object(
    *,
    object_id: str,
    declaration: dict[str, Any],
    capsule_path: Path,
    vault_root: Path,
    staging_root: Path,
) -> MaterializedObject:
    try:
        source_spec = resolve_capsule_object(
            capsule_path=capsule_path,
            object_id=object_id,
            vault_root=vault_root,
        )
    except VerifyError as exc:
        raise MaterializationError(
            f"Cannot resolve object {object_id!r}: {exc}"
        ) from exc

    before = source_spec.path.stat()

    source_verification = verify_object(source_spec)
    if not source_verification.verified:
        raise MaterializationError(
            f"Object failed verification before materialization: {object_id}"
        )

    format_name = declaration.get("format")
    object_root = staging_root / "objects" / object_id

    member_count = None
    regular_bytes = None
    symlink_count = None
    hardlink_count = None

    if format_name in {"tar", "tar.gz"}:
        try:
            extraction = extract_tar_safely(
                archive_path=source_spec.path,
                destination=object_root,
            )
        except SafeTarError as exc:
            raise MaterializationError(
                f"Safe extraction failed for {object_id!r}: {exc}"
            ) from exc

        strategy = "extract"
        destination = f"objects/{object_id}"
        member_count = extraction.member_count
        regular_bytes = extraction.regular_bytes
        symlink_count = extraction.symlink_count
        hardlink_count = extraction.hardlink_count
    elif format_name == "file":
        destination_path = object_root / "payload"
        written = _copy_regular_object(
            source_spec=source_spec,
            destination=destination_path,
        )
        strategy = "copy"
        destination = f"objects/{object_id}/payload"
        member_count = 1
        regular_bytes = written
        symlink_count = 0
        hardlink_count = 0
    else:
        raise MaterializationError(
            f"Unsupported materialization format for {object_id!r}: "
            f"{format_name!r}"
        )

    after = source_spec.path.stat()
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
        if getattr(before, field, None) != getattr(after, field, None):
            raise MaterializationError(
                f"Vault object changed during materialization: {object_id}"
            )

    post_verification = verify_object(source_spec)
    if not post_verification.verified:
        raise MaterializationError(
            f"Vault object failed post-materialization verification: "
            f"{object_id}"
        )

    return MaterializedObject(
        object_id=object_id,
        digest=source_spec.expected_digest,
        destination=destination,
        strategy=strategy,
        verified=True,
        member_count=member_count,
        regular_bytes=regular_bytes,
        symlink_count=symlink_count,
        hardlink_count=hardlink_count,
    )


def _receipt_document(
    *,
    capsule_id: str,
    profile_id: str,
    objects: tuple[MaterializedObject, ...],
    persistent_state: list[dict[str, Any]],
) -> dict[str, Any]:
    receipt_id = f"materialization-{uuid.uuid4()}"

    receipt_state = []
    must_preserve = []

    for item in persistent_state:
        item_id = item.get("id")
        item_path = item.get("path")
        backup = item.get("backup", True)

        if not isinstance(item_id, str) or not item_id:
            raise MaterializationError(
                "Persistent-state entry has no valid id."
            )
        if not isinstance(item_path, str) or not item_path:
            raise MaterializationError(
                f"Persistent-state {item_id!r} has no valid path."
            )

        _safe_relative_path(
            item_path,
            f"persistent_state[{item_id!r}].path",
        )

        receipt_state.append(
            {
                "id": item_id,
                "path": item_path,
                "preserve_on_remove": bool(backup),
            }
        )
        if backup:
            must_preserve.append(item_path)

    receipt_objects = [
        {
            "id": item.object_id,
            "digest": item.digest,
            "destination": item.destination,
            "strategy": item.strategy,
            "verified": item.verified,
        }
        for item in objects
    ]

    operations = [
        {
            "id": "verify-profile",
            "type": "verify",
            "status": "completed",
            "details": (
                f"Verified {len(objects)} profile object(s) "
                "before materialization."
            ),
        }
    ]

    for item in objects:
        details = (
            f"{item.object_id}: {item.strategy}; "
            f"members={item.member_count}; "
            f"regular_bytes={item.regular_bytes}; "
            f"symlinks={item.symlink_count}; "
            f"hardlinks={item.hardlink_count}"
        )
        operations.append(
            {
                "id": f"materialize-{item.object_id}",
                "type": item.strategy,
                "status": "completed",
                "details": details,
            }
        )

    operations.append(
        {
            "id": "write-receipt",
            "type": "generate",
            "status": "completed",
            "details": RECEIPT_NAME,
        }
    )

    return {
        "schema": 0,
        "receipt_id": receipt_id,
        "capsule_id": capsule_id,
        "profile_id": profile_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "orchestrator_version": __version__,
        "host_summary": {
            "platform": sys.platform,
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "destination": ".",
        "objects": receipt_objects,
        "operations": operations,
        "persistent_state": receipt_state,
        "removal": {
            "safe_to_remove": [
                "objects",
                RECEIPT_NAME,
            ],
            "must_preserve": must_preserve,
        },
    }


def _write_receipt(
    *,
    staging_root: Path,
    document: dict[str, Any],
) -> None:
    receipt_path = staging_root / RECEIPT_NAME
    serialized = (
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )

    try:
        with receipt_path.open(
            "x",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        descriptor = os.open(staging_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MaterializationError(
            f"Cannot write materialization receipt: {exc}"
        ) from exc


def materialize_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
    destination: Path,
) -> MaterializationResult:
    """Verify, stage, and atomically publish one materialization."""

    capsule_path = capsule_path.expanduser().absolute()
    vault_root = vault_root.expanduser().resolve()
    destination = _canonical_destination(destination)

    try:
        plan = build_plan(
            capsule_path=capsule_path,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=destination,
        )
    except PlanError as exc:
        raise MaterializationError(str(exc)) from exc

    verification = verify_profile(
        capsule_path=capsule_path,
        profile_id=profile_id,
        vault_root=vault_root,
    )
    if not verification.verified:
        failed = ", ".join(
            item.object_id
            for item in verification.objects
            if not item.verified
        )
        raise MaterializationError(
            "Profile is not fully verified in the vault: "
            f"{failed}"
        )

    capsule = _load_capsule(capsule_path)
    profile, persistent_state = _profile_and_state(
        capsule=capsule,
        profile_id=profile_id,
    )
    definition = load_profile_definition(
        capsule_path=capsule_path,
        profile_id=profile_id,
    )

    if destination.exists() or destination.is_symlink():
        raise MaterializationError(
            f"Destination already exists: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock, lock_descriptor = _acquire_lock(destination)
    staging = destination.parent / (
        f".ogv-stage-{destination.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )

    objects: list[MaterializedObject] = []
    promoted = False

    try:
        if destination.exists() or destination.is_symlink():
            raise MaterializationError(
                f"Destination appeared after locking: {destination}"
            )

        staging.mkdir(mode=0o700)

        dependencies = profile.get("dependencies")
        if not isinstance(dependencies, list):
            raise MaterializationError(
                "profile.dependencies must be an array"
            )

        for object_id in dependencies:
            if not isinstance(object_id, str):
                raise MaterializationError(
                    "Profile dependency ID must be a string."
                )

            declaration = definition.object_index[object_id]
            objects.append(
                _materialize_object(
                    object_id=object_id,
                    declaration=declaration,
                    capsule_path=capsule_path,
                    vault_root=vault_root,
                    staging_root=staging,
                )
            )

        object_tuple = tuple(objects)
        receipt = _receipt_document(
            capsule_id=plan.capsule_id,
            profile_id=profile_id,
            objects=object_tuple,
            persistent_state=persistent_state,
        )
        _write_receipt(
            staging_root=staging,
            document=receipt,
        )

        _rename_noreplace(staging, destination)
        promoted = True

        return MaterializationResult(
            schema=0,
            receipt_id=receipt["receipt_id"],
            capsule_id=plan.capsule_id,
            profile_id=profile_id,
            destination=str(destination),
            object_count=len(object_tuple),
            complete=True,
            objects=object_tuple,
        )
    except (
        OSError,
        ProfileStoreError,
        VerifyError,
        SafeTarError,
    ) as exc:
        raise MaterializationError(str(exc)) from exc
    finally:
        if not promoted and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _release_lock(lock, lock_descriptor)


def _load_receipt(destination: Path) -> dict[str, Any]:
    receipt_path = destination / RECEIPT_NAME

    if receipt_path.is_symlink():
        raise MaterializationError(
            f"Receipt must not be a symbolic link: {receipt_path}"
        )

    try:
        document = json.loads(
            receipt_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise MaterializationError(
            f"Materialization receipt not found: {receipt_path}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MaterializationError(
            f"Receipt is not valid UTF-8: {receipt_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MaterializationError(
            f"Invalid receipt JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise MaterializationError(
            "Receipt top-level value must be an object."
        )
    if document.get("destination") != ".":
        raise MaterializationError(
            "Receipt is not anchored to its own directory."
        )

    return document


def remove_materialization(
    *,
    destination: Path,
    confirm_state_preserved: bool,
) -> RemovalResult:
    """Atomically detach and then remove one recognized materialization."""

    destination = _canonical_destination(destination)

    if destination.is_symlink():
        raise MaterializationError(
            f"Materialization destination must not be a symlink: "
            f"{destination}"
        )
    if not destination.is_dir():
        raise MaterializationError(
            f"Materialization destination is not a directory: "
            f"{destination}"
        )

    receipt = _load_receipt(destination)
    capsule_id = receipt.get("capsule_id")
    profile_id = receipt.get("profile_id")

    if not isinstance(capsule_id, str) or not capsule_id:
        raise MaterializationError(
            "Receipt has no valid capsule_id."
        )
    if not isinstance(profile_id, str) or not profile_id:
        raise MaterializationError(
            "Receipt has no valid profile_id."
        )

    state = receipt.get("persistent_state")
    if not isinstance(state, list):
        raise MaterializationError(
            "Receipt persistent_state must be an array."
        )

    preserve = [
        item
        for item in state
        if isinstance(item, dict)
        and item.get("preserve_on_remove") is True
    ]
    if preserve and not confirm_state_preserved:
        ids = ", ".join(
            str(item.get("id", "unknown"))
            for item in preserve
        )
        raise MaterializationError(
            "Persistent state must be preserved before removal: "
            f"{ids}. Re-run only after backup with "
            "--confirm-state-preserved."
        )

    removal = receipt.get("removal")
    if not isinstance(removal, dict):
        raise MaterializationError(
            "Receipt has no valid removal declaration."
        )

    safe_to_remove = removal.get("safe_to_remove")
    if not isinstance(safe_to_remove, list):
        raise MaterializationError(
            "Receipt removal.safe_to_remove must be an array."
        )

    allowed_top_level = set()
    for value in safe_to_remove:
        if not isinstance(value, str):
            raise MaterializationError(
                "safe_to_remove entries must be strings."
            )
        relative = _safe_relative_path(
            value,
            "removal.safe_to_remove",
        )
        allowed_top_level.add(relative.parts[0])

    actual_top_level = {
        item.name for item in destination.iterdir()
    }
    unknown = sorted(actual_top_level - allowed_top_level)
    if unknown:
        raise MaterializationError(
            "Destination contains paths not declared safe to remove: "
            + ", ".join(unknown)
        )

    lock, lock_descriptor = _acquire_lock(destination)
    detached = destination.parent / (
        f".ogv-remove-{destination.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )

    try:
        _rename_noreplace(destination, detached)
        try:
            shutil.rmtree(detached)
        except OSError as exc:
            raise MaterializationError(
                "Materialization was detached but could not be fully "
                f"removed; inspect {detached}: {exc}"
            ) from exc
    finally:
        _release_lock(lock, lock_descriptor)

    return RemovalResult(
        schema=0,
        capsule_id=capsule_id,
        profile_id=profile_id,
        destination=str(destination),
        removed=True,
        persistent_state_declared=len(preserve),
        state_preservation_confirmed=confirm_state_preserved,
    )
