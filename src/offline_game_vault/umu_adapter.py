"""Verified modular UMU materialization and execution.

The adapter keeps immutable objects, capsule launchers, and persistent state
separate. It supports normalized tar, tar.gz, and tar.zst inputs whose members
already use their final portable relative layout.

Security model:
- every immutable object is verified through the core verifier;
- every state archive and capsule asset is verified by SHA-256;
- archives receive a complete path/link preflight before extraction;
- extraction occurs only in a private staging directory;
- final publication is atomic and non-overwriting;
- launchers live in the capsule, never in the immutable game object;
- the launcher remains responsible for the verified network-isolation contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Sequence
import uuid

from . import __version__
from .materializer import (
    _acquire_lock,
    _canonical_destination,
    _release_lock,
    _rename_noreplace,
)
from .verifier import (
    VerifyError,
    resolve_capsule_object,
    verify_object,
)
from .composition_state import (
    CompositionStateError,
    prepare_composition_state,
    restore_composition_state,
    verify_composition_state_evidence,
)

RECEIPT_NAME = "umu-materialization.json"
LAST_RUN_RECEIPT = "receipts/last-umu-run.json"
PORTABLE_RUNTIME_DESTINATION = "metadata/ogv_umu_runtime.py"
ROOT_LAUNCHER = "JUGAR.sh"
ROOT_VERIFIER = "VERIFICAR.sh"
ROOT_UNINSTALLER = "DESINSTALAR.sh"

_RUNTIME_PLATFORM_PREFIX = {
    "steamrt2": "soldier",
    "steamrt3": "sniper",
    "steamrt4": "steamrt4",
    "steamrt4-arm64": "steamrt4-arm64",
}


def _runtime_platform_prefix(family: str) -> str:
    try:
        return _RUNTIME_PLATFORM_PREFIX[family]
    except KeyError as exc:
        raise UmuAdapterError(
            f"Unsupported preserved Steam runtime family: {family}."
        ) from exc


class UmuAdapterError(Exception):
    """Raised when a modular UMU operation cannot proceed safely."""


@dataclass(frozen=True)
class UmuMaterializationResult:
    schema: int
    receipt_id: str
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    object_count: int
    selected_save: str | None
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UmuVerificationResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    protected_file_count: int
    symlink_count: int
    hardlink_group_count: int
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UmuRunResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    process_rc: int
    duration_ms: int
    sanitizer_rc: int
    verified_after_run: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UmuRemovalResult:
    schema: int
    capsule_id: str
    profile_id: str
    backend: str
    destination: str
    selected_save: str | None
    state_preservation_confirmed: bool
    removed: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    kind: str
    linkname: str | None


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(
            character not in "0123456789abcdef"
            for character in value.removeprefix("sha256:")
        )
    ):
        raise UmuAdapterError(
            f"{field} must use lowercase sha256:<64 hex> form."
        )
    return value.removeprefix("sha256:")


def _safe_relative(value: Any, field: str, *, dot: bool = False) -> PurePosixPath:
    if dot and value == ".":
        return PurePosixPath(".")
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise UmuAdapterError(f"{field} is not a portable relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UmuAdapterError(f"{field} is not a safe relative path.")
    return path


def _path_under(root: Path, relative: PurePosixPath) -> Path:
    candidate = root if relative == PurePosixPath(".") else root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise UmuAdapterError("A declared path escapes its root.") from exc
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UmuAdapterError(f"{label} is absent or not a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise UmuAdapterError(f"{label} is not valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise UmuAdapterError(f"{label} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise UmuAdapterError(f"{label} must contain a JSON object.")
    return value


def _profile_contract(
    capsule_path: Path,
    profile_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    capsule = _load_json(capsule_path, "Capsule")
    capsule_id = capsule.get("capsule_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        raise UmuAdapterError("capsule_id is absent or invalid.")

    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise UmuAdapterError("capsule.profiles must be an array.")
    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if len(matches) != 1:
        raise UmuAdapterError(
            "The requested profile must exist exactly once."
        )
    profile = matches[0]
    if profile.get("adapter") != "umu":
        raise UmuAdapterError("The requested profile is not adapter='umu'.")
    if profile.get("platform") != "linux":
        raise UmuAdapterError("UMU materialization requires platform='linux'.")
    contract = profile.get("umu")
    if not isinstance(contract, dict):
        raise UmuAdapterError("The requested profile has no UMU contract.")
    if contract.get("schema") != 0:
        raise UmuAdapterError("Unsupported UMU contract schema.")
    return capsule, profile, contract


def _allowed_absolute_links(
    contract: dict[str, Any],
) -> set[tuple[str, str]]:
    raw = contract.get("allowed_absolute_symlinks", [])
    if not isinstance(raw, list):
        raise UmuAdapterError(
            "umu.allowed_absolute_symlinks must be an array."
        )
    result: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise UmuAdapterError(
                f"umu.allowed_absolute_symlinks[{index}] must be an object."
            )
        path = _safe_relative(
            item.get("path"),
            f"umu.allowed_absolute_symlinks[{index}].path",
        ).as_posix()
        target = item.get("target")
        if not isinstance(target, str) or not target.startswith("/"):
            raise UmuAdapterError(
                f"umu.allowed_absolute_symlinks[{index}].target "
                "must be absolute."
            )
        result.add((path, target))
    return result


def _validate_member_name(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise UmuAdapterError("Archive contains an unsafe member name.")
    normalized = value.rstrip("/")
    if not normalized:
        raise UmuAdapterError("Archive contains an empty member name.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UmuAdapterError(
            f"Archive member is not a safe relative path: {value!r}"
        )
    return path.as_posix()


def _validate_link(
    *,
    name: str,
    target: str,
    allowed_absolute: set[tuple[str, str]],
    allow_absolute: bool,
) -> None:
    if "\x00" in target or "\\" in target:
        raise UmuAdapterError(f"Unsafe symlink target: {name}")
    if target.startswith("/"):
        if not allow_absolute and (name, target) not in allowed_absolute:
            raise UmuAdapterError(
                f"Unexpected absolute symlink: {name} -> {target}"
            )
        return
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(name), target)
    )
    if resolved == ".." or resolved.startswith("../"):
        raise UmuAdapterError(
            f"Symlink escapes the materialization: {name} -> {target}"
        )


def _validate_hardlink_target(*, name: str, target: str) -> str:
    if "\x00" in target or "\\" in target:
        raise UmuAdapterError(f"Unsafe hardlink target: {name}")
    try:
        return _validate_member_name(target)
    except UmuAdapterError as exc:
        raise UmuAdapterError(
            f"Hardlink target is not a safe archive path: {name} -> {target}"
        ) from exc


def _member_parent_names(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    return tuple(
        parent.as_posix()
        for parent in path.parents
        if parent != PurePosixPath(".")
    )


def _inspect_tar_stream(
    stream: BinaryIO,
    *,
    allowed_absolute: set[tuple[str, str]],
    allow_absolute_symlinks: bool,
    allow_hardlinks: bool,
    mode: str = "r|",
) -> tuple[ArchiveMember, ...]:
    members: list[ArchiveMember] = []
    seen_non_dirs: set[str] = set()

    with tarfile.open(fileobj=stream, mode=mode) as archive:
        for member in archive:
            name = _validate_member_name(member.name)

            if member.isdir():
                kind = "directory"
                linkname = None
            elif member.isfile():
                kind = "regular"
                linkname = None
                if name in seen_non_dirs:
                    raise UmuAdapterError(
                        f"Duplicate non-directory archive member: {name}"
                    )
                seen_non_dirs.add(name)
            elif member.issym():
                kind = "symlink"
                linkname = member.linkname
                _validate_link(
                    name=name,
                    target=member.linkname,
                    allowed_absolute=allowed_absolute,
                    allow_absolute=allow_absolute_symlinks,
                )
                if name in seen_non_dirs:
                    raise UmuAdapterError(
                        f"Duplicate non-directory archive member: {name}"
                    )
                seen_non_dirs.add(name)
            elif member.islnk():
                if not allow_hardlinks:
                    raise UmuAdapterError(
                        f"Hardlinks are not allowed for this object: {name}"
                    )
                kind = "hardlink"
                linkname = _validate_hardlink_target(
                    name=name,
                    target=member.linkname,
                )
                if name in seen_non_dirs:
                    raise UmuAdapterError(
                        f"Duplicate non-directory archive member: {name}"
                    )
                seen_non_dirs.add(name)
            else:
                raise UmuAdapterError(
                    f"Special archive member is not supported: {name}"
                )

            members.append(ArchiveMember(name, kind, linkname))

    if not members:
        raise UmuAdapterError("Archive is empty.")

    regular_names = {
        member.name
        for member in members
        if member.kind == "regular"
    }
    link_names = {
        member.name
        for member in members
        if member.kind in {"symlink", "hardlink"}
    }

    for member in members:
        if member.kind == "hardlink":
            assert member.linkname is not None
            if member.linkname not in regular_names:
                raise UmuAdapterError(
                    "Hardlink target is not a regular archive member: "
                    f"{member.name} -> {member.linkname}"
                )

        linked_parent = next(
            (
                parent
                for parent in _member_parent_names(member.name)
                if parent in link_names
            ),
            None,
        )
        if linked_parent is not None:
            raise UmuAdapterError(
                "Archive member is nested below a link member: "
                f"{member.name} below {linked_parent}"
            )

    return tuple(members)



def _archive_format(
    archive_path: Path,
    declared_format: str | None,
) -> str:
    if declared_format is not None:
        if declared_format not in {"tar", "tar.gz", "tar.zst"}:
            raise UmuAdapterError(
                f"Unsupported declared archive format: {declared_format}"
            )
        return declared_format
    if archive_path.name.endswith(".tar.zst"):
        return "tar.zst"
    if archive_path.name.endswith(".tar.gz"):
        return "tar.gz"
    if archive_path.name.endswith(".tar"):
        return "tar"
    raise UmuAdapterError(
        f"Archive format is not declared and cannot be inferred: "
        f"{archive_path.name}"
    )


def _inspect_archive(
    archive_path: Path,
    *,
    allowed_absolute: set[tuple[str, str]],
    allow_absolute_symlinks: bool = False,
    allow_hardlinks: bool = False,
    declared_format: str | None = None,
) -> tuple[ArchiveMember, ...]:
    archive_format = _archive_format(archive_path, declared_format)
    if archive_format == "tar.zst":
        zstd = shutil.which("zstd")
        if zstd is None:
            raise UmuAdapterError("zstd is required for tar.zst objects.")
        process = subprocess.Popen(
            [zstd, "-dc", "--no-progress", str(archive_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            members = _inspect_tar_stream(
                process.stdout,
                allowed_absolute=allowed_absolute,
                allow_absolute_symlinks=allow_absolute_symlinks,
                allow_hardlinks=allow_hardlinks,
            )
            stderr = process.stderr.read().decode(
                "utf-8", errors="replace"
            )
            returncode = process.wait()
            if returncode != 0:
                raise UmuAdapterError(
                    f"zstd preflight failed ({returncode}): "
                    f"{stderr.strip()}"
                )
            return members
        except Exception:
            process.kill()
            process.wait()
            raise

    mode = "r|gz" if archive_format == "tar.gz" else "r|"
    try:
        with archive_path.open("rb") as stream:
            return _inspect_tar_stream(
                stream,
                allowed_absolute=allowed_absolute,
                allow_absolute_symlinks=allow_absolute_symlinks,
                allow_hardlinks=allow_hardlinks,
                mode=mode,
            )
    except (OSError, tarfile.TarError) as exc:
        raise UmuAdapterError(
            f"Cannot inspect archive {archive_path.name}: {exc}"
        ) from exc

def _check_collisions(
    destination: Path,
    members: Iterable[ArchiveMember],
) -> None:
    for member in members:
        path = destination.joinpath(*PurePosixPath(member.name).parts)
        if not (path.exists() or path.is_symlink()):
            continue
        if member.kind == "directory" and path.is_dir() and not path.is_symlink():
            continue
        raise UmuAdapterError(
            f"Archive member would overwrite an existing path: {member.name}"
        )


def _extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    allowed_absolute: set[tuple[str, str]],
    allow_absolute_symlinks: bool = False,
    allow_hardlinks: bool = False,
    declared_format: str | None = None,
) -> tuple[ArchiveMember, ...]:
    archive_format = _archive_format(archive_path, declared_format)
    members = _inspect_archive(
        archive_path,
        allowed_absolute=allowed_absolute,
        allow_absolute_symlinks=allow_absolute_symlinks,
        allow_hardlinks=allow_hardlinks,
        declared_format=archive_format,
    )
    _check_collisions(destination, members)
    tar_binary = shutil.which("tar")
    if tar_binary is None:
        raise UmuAdapterError("GNU tar is required for UMU extraction.")
    command = [
        tar_binary,
        "--extract",
        "--file",
        str(archive_path),
        "--directory",
        str(destination),
        "--same-permissions",
        "--no-same-owner",
        "--no-xattrs",
        "--no-acls",
        "--no-selinux",
    ]
    if archive_format == "tar.zst":
        command.insert(1, "--zstd")
    elif archive_format == "tar.gz":
        command.insert(1, "--gzip")
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise UmuAdapterError(
            f"Archive extraction failed: {exc.stderr.strip()}"
        ) from exc
    return members

def _merge_mapping(
    *,
    object_root: Path,
    staging_root: Path,
    source_value: Any,
    destination_value: Any,
    field: str,
) -> None:
    source_relative = _safe_relative(
        source_value, f"{field}.source", dot=True
    )
    destination_relative = _safe_relative(
        destination_value, f"{field}.destination", dot=True
    )
    source = _path_under(object_root, source_relative)
    destination = _path_under(staging_root, destination_relative)
    if not (source.exists() or source.is_symlink()):
        raise UmuAdapterError(f"{field}.source does not exist.")
    if destination.exists() or destination.is_symlink():
        raise UmuAdapterError(
            f"{field}.destination already exists: {destination_relative}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _verify_regular_file(
    path: Path,
    expected_digest: str,
    *,
    expected_size: int | None = None,
    label: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise UmuAdapterError(f"{label} is absent or not a regular file.")
    actual_size = path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise UmuAdapterError(
            f"{label} size mismatch: {actual_size} != {expected_size}"
        )
    actual_digest = _sha256_file(path)
    if actual_digest != expected_digest:
        raise UmuAdapterError(
            f"{label} SHA-256 mismatch: {actual_digest} != {expected_digest}"
        )


def _copy_capsule_asset(
    *,
    capsule_root: Path,
    staging_root: Path,
    item: dict[str, Any],
    context: str,
) -> dict[str, Any]:
    source_relative = _safe_relative(
        item.get("source"), f"{context}.source"
    )
    destination_relative = _safe_relative(
        item.get("destination"), f"{context}.destination"
    )
    expected = _parse_digest(item.get("digest"), f"{context}.digest")
    mode = item.get("mode", 0o755)
    if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
        raise UmuAdapterError(f"{context}.mode is invalid.")

    source = _path_under(capsule_root, source_relative)
    destination = _path_under(staging_root, destination_relative)
    _verify_regular_file(source, expected, label=context)
    if destination.exists() or destination.is_symlink():
        raise UmuAdapterError(f"{context}.destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)
    _verify_regular_file(destination, expected, label=context)
    return {
        "path": destination_relative.as_posix(),
        "digest": f"sha256:{expected}",
        "mode": mode,
    }


def _parse_hash_manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise UmuAdapterError(
            f"Cannot read protected manifest {path.name}: {exc}"
        ) from exc
    for number, raw in enumerate(lines, 1):
        if not raw:
            continue
        try:
            digest, value = raw.split("  ", 1)
        except ValueError as exc:
            raise UmuAdapterError(
                f"Malformed manifest line {path.name}:{number}"
            ) from exc
        value = value.removeprefix("./")
        relative = _safe_relative(
            value, f"{path.name}:{number}"
        ).as_posix()
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise UmuAdapterError(
                f"Malformed manifest digest {path.name}:{number}"
            )
        if relative in result:
            raise UmuAdapterError(
                f"Duplicate path in manifest {path.name}: {relative}"
            )
        result[relative] = digest
    if not result:
        raise UmuAdapterError(f"Protected manifest is empty: {path.name}")
    return result


def _parse_symlink_manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise UmuAdapterError(
            f"Cannot read symlink manifest {path.name}: {exc}"
        ) from exc
    for number, raw in enumerate(lines, 1):
        if not raw:
            continue
        try:
            value, target = raw.split("\t", 1)
        except ValueError as exc:
            raise UmuAdapterError(
                f"Malformed symlink line {path.name}:{number}"
            ) from exc
        relative = _safe_relative(
            value.removeprefix("./"), f"{path.name}:{number}"
        ).as_posix()
        if relative in result:
            raise UmuAdapterError(
                f"Duplicate symlink in {path.name}: {relative}"
            )
        result[relative] = target
    return result


def _copy_verification_asset(
    *,
    capsule_root: Path,
    metadata_root: Path,
    source_value: Any,
    context: str,
) -> tuple[Path, str]:
    source_relative = _safe_relative(source_value, context)
    source = _path_under(capsule_root, source_relative)
    if source.is_symlink() or not source.is_file():
        raise UmuAdapterError(f"{context} is absent.")
    destination = metadata_root / source_relative.name
    if destination.exists():
        raise UmuAdapterError(
            f"Duplicate verification asset name: {destination.name}"
        )
    shutil.copyfile(source, destination)
    digest = _sha256_file(destination)
    return destination, digest


def _collect_symlinks(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        for name in list(directories) + list(files):
            path = current_path / name
            if path.is_symlink():
                result[path.relative_to(root).as_posix()] = os.readlink(path)
    return result


def _verify_no_broken_symlinks(
    root: Path,
    *,
    allowed_unresolved_prefixes: set[str],
) -> None:
    for relative in _collect_symlinks(root):
        if any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in allowed_unresolved_prefixes
        ):
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.exists():
            raise UmuAdapterError(f"Broken symlink: {relative}")


def _runtime_context_unresolved_prefixes(
    root: Path,
    runtime_var_relative: PurePosixPath,
) -> set[str]:
    # Return concrete pressure-vessel temporary roots under runtime var.
    runtime_var = _path_under(
        root,
        runtime_var_relative,
    )

    if (
        runtime_var.is_symlink()
        or not runtime_var.is_dir()
    ):
        raise UmuAdapterError(
            "Runtime var is absent or linked."
        )

    try:
        entries = sorted(
            runtime_var.iterdir(),
            key=lambda item: item.name,
        )
    except OSError as exc:
        raise UmuAdapterError(
            "Cannot inspect runtime var."
        ) from exc

    prefixes: set[str] = set()

    for entry in entries:
        if re.fullmatch(
            r"tmp-[A-Za-z0-9._-]+",
            entry.name,
        ) is None:
            continue

        if (
            entry.is_symlink()
            or not entry.is_dir()
        ):
            raise UmuAdapterError(
                "Runtime temporary entry is not a regular directory: "
                f"{entry.name}"
            )

        prefixes.add(
            entry.relative_to(root).as_posix()
        )

    return prefixes


def _parse_hardlink_manifest(path: Path) -> list[list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UmuAdapterError(
            f"Cannot read hardlink manifest {path.name}: {exc}"
        ) from exc
    if not isinstance(value, list):
        raise UmuAdapterError(
            f"Hardlink manifest must contain an array: {path.name}"
        )

    groups: list[list[str]] = []
    seen_paths: set[str] = set()
    for group_index, group in enumerate(value):
        if (
            not isinstance(group, list)
            or len(group) < 2
            or any(not isinstance(item, str) for item in group)
        ):
            raise UmuAdapterError(
                f"Invalid hardlink group {path.name}:{group_index}"
            )
        normalized = sorted(
            _safe_relative(
                item,
                f"{path.name}:{group_index}",
            ).as_posix()
            for item in group
        )
        if len(normalized) != len(set(normalized)):
            raise UmuAdapterError(
                f"Duplicate path in hardlink group {path.name}:{group_index}"
            )
        overlap = seen_paths & set(normalized)
        if overlap:
            raise UmuAdapterError(
                f"Path appears in multiple hardlink groups: {sorted(overlap)[0]}"
            )
        seen_paths.update(normalized)
        groups.append(normalized)

    if groups != sorted(groups):
        raise UmuAdapterError(
            f"Hardlink manifest is not canonically sorted: {path.name}"
        )
    return groups


def _collect_hardlink_groups(root: Path) -> list[list[str]]:
    inode_paths: dict[tuple[int, int], list[str]] = {}
    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = current_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 2:
                continue
            relative = path.relative_to(root).as_posix()
            inode_paths.setdefault(
                (metadata.st_dev, metadata.st_ino),
                [],
            ).append(relative)

    return sorted(
        sorted(paths)
        for paths in inode_paths.values()
        if len(paths) > 1
    )


def _verify_hardlink_sets(
    root: Path,
    manifests: list[dict[str, Any]],
) -> int:
    verified = 0
    for item in manifests:
        manifest_path = root / "metadata/verification" / item["name"]
        if _sha256_file(manifest_path) != item["digest"]:
            raise UmuAdapterError(
                f"Hardlink manifest changed: {item['name']}"
            )
        expected = _parse_hardlink_manifest(manifest_path)
        relative_root = _safe_relative(
            item.get("root"),
            f"hardlink manifest {item['name']}.root",
        )
        target_root = _path_under(root, relative_root)
        if target_root.is_symlink() or not target_root.is_dir():
            raise UmuAdapterError(
                f"Hardlink root is absent or linked: {relative_root}"
            )
        actual = _collect_hardlink_groups(target_root)
        if actual != expected:
            raise UmuAdapterError(
                f"Hardlink topology mismatch for {item['name']}: "
                f"expected={len(expected)}, actual={len(actual)}"
            )
        verified += len(expected)
    return verified


def _verify_manifest_set(
    root: Path,
    manifests: list[dict[str, Any]],
    *,
    skip_paths: set[str],
) -> int:
    verified = 0
    for item in manifests:
        manifest_path = root / "metadata/verification" / item["name"]
        if _sha256_file(manifest_path) != item["digest"]:
            raise UmuAdapterError(
                f"Verification manifest changed: {item['name']}"
            )
        entries = _parse_hash_manifest(manifest_path)
        for relative, expected in entries.items():
            if relative in skip_paths:
                continue
            path = root.joinpath(*PurePosixPath(relative).parts)
            _verify_regular_file(
                path,
                expected,
                label=f"Protected file {relative}",
            )
            verified += 1
    return verified


def _verify_symlink_sets(
    root: Path,
    manifests: list[dict[str, Any]],
) -> int:
    actual_all = _collect_symlinks(root)
    verified = 0
    for item in manifests:
        manifest_path = root / "metadata/verification" / item["name"]
        if _sha256_file(manifest_path) != item["digest"]:
            raise UmuAdapterError(
                f"Symlink manifest changed: {item['name']}"
            )
        expected = _parse_symlink_manifest(manifest_path)
        prefixes = item["prefixes"]
        actual = {
            path: target
            for path, target in actual_all.items()
            if any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in prefixes
            )
        }
        if actual != expected:
            missing = len(set(expected) - set(actual))
            extra = len(set(actual) - set(expected))
            wrong = sum(
                actual[path] != expected[path]
                for path in set(actual) & set(expected)
            )
            raise UmuAdapterError(
                f"Symlink set mismatch for {item['name']}: "
                f"missing={missing}, extra={extra}, wrong={wrong}"
            )
        verified += len(expected)
    return verified


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
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
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_receipt(destination: Path) -> dict[str, Any]:
    receipt = _load_json(destination / RECEIPT_NAME, "UMU receipt")
    if receipt.get("schema") != 0 or receipt.get("backend") != "umu":
        raise UmuAdapterError("Unsupported or non-UMU materialization receipt.")
    if receipt.get("destination") != ".":
        raise UmuAdapterError("UMU receipt is not self-anchored.")
    return receipt


def _state_archive(
    *,
    contract: dict[str, Any],
    state_root: Path | None,
    save_id: str | None,
) -> tuple[list[tuple[dict[str, Any], Path]], str | None]:
    raw = contract.get("state_archives", [])
    if not isinstance(raw, list):
        raise UmuAdapterError("umu.state_archives must be an array.")
    selected: list[tuple[dict[str, Any], Path]] = []
    selectable_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise UmuAdapterError(
                f"umu.state_archives[{index}] must be an object."
            )
        item_id = item.get("id")
        filename = item.get("filename")
        policy = item.get("policy")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(filename, str)
            or not filename
            or policy not in {"always", "selectable"}
        ):
            raise UmuAdapterError(
                f"umu.state_archives[{index}] is invalid."
            )
        _parse_digest(
            item.get("digest"),
            f"umu.state_archives[{index}].digest",
        )
        if policy == "selectable":
            selectable_ids.add(item_id)
        if policy == "always" or item_id == save_id:
            if state_root is None:
                raise UmuAdapterError(
                    "--state-root is required by the UMU contract."
                )
            path = state_root / filename
            selected.append((item, path))
    if save_id is not None and save_id not in selectable_ids:
        raise UmuAdapterError(f"Unknown UMU save selection: {save_id!r}")
    return selected, save_id



def _layout_archive_policies(
    layout: list[Any],
) -> dict[str, dict[str, bool]]:
    policies: dict[str, dict[str, bool]] = {}

    for index, item in enumerate(layout):
        if not isinstance(item, dict):
            raise UmuAdapterError(
                f"umu.layout[{index}] must be an object."
            )
        object_id = item.get("object")
        if not isinstance(object_id, str) or not object_id:
            raise UmuAdapterError(
                f"umu.layout[{index}].object is invalid."
            )

        raw = item.get("archive_policy", {})
        if not isinstance(raw, dict):
            raise UmuAdapterError(
                f"umu.layout[{index}].archive_policy must be an object."
            )

        unknown = set(raw) - {
            "allow_absolute_symlinks",
            "allow_hardlinks",
        }
        if unknown:
            raise UmuAdapterError(
                f"umu.layout[{index}].archive_policy has unknown keys: "
                + ", ".join(sorted(unknown))
            )

        policy = {
            "allow_absolute_symlinks":
                raw.get("allow_absolute_symlinks", False),
            "allow_hardlinks":
                raw.get("allow_hardlinks", False),
        }

        if any(
            not isinstance(value, bool)
            for value in policy.values()
        ):
            raise UmuAdapterError(
                f"umu.layout[{index}].archive_policy values must be boolean."
            )

        previous = policies.get(object_id)
        if previous is not None and previous != policy:
            raise UmuAdapterError(
                f"All layout mappings for {object_id!r} "
                "must use the same archive_policy."
            )
        policies[object_id] = policy

    return policies


def _verify_required_runtime_path(
    runtime_root: Path,
    item: dict[str, Any],
    *,
    index: int,
) -> dict[str, str]:
    relative = _safe_relative(
        item.get("path"),
        f"umu.offline_environment.runtime.required_paths[{index}].path",
    )
    expected_type = item.get("type")
    if expected_type not in {"directory", "file"}:
        raise UmuAdapterError(
            "umu.offline_environment.runtime.required_paths"
            f"[{index}].type is invalid."
        )
    path = _path_under(runtime_root, relative)
    if path.is_symlink():
        raise UmuAdapterError(
            f"Required runtime path is linked: {relative}"
        )
    if expected_type == "directory" and not path.is_dir():
        raise UmuAdapterError(
            f"Required runtime directory is absent: {relative}"
        )
    if expected_type == "file" and not path.is_file():
        raise UmuAdapterError(
            f"Required runtime file is absent: {relative}"
        )
    return {
        "path": relative.as_posix(),
        "type": expected_type,
    }


def _verify_offline_environment(
    root: Path,
    raw: Any,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UmuAdapterError(
            "umu.offline_environment must be an object."
        )

    required_keys = {
        "xdg_data_home",
        "xdg_cache_home",
        "runtime_update",
        "runtime",
        "cache_entries",
    }
    if set(raw) != required_keys:
        raise UmuAdapterError(
            "umu.offline_environment keys are incomplete or unknown."
        )

    xdg_data_relative = _safe_relative(
        raw.get("xdg_data_home"),
        "umu.offline_environment.xdg_data_home",
    )
    xdg_cache_relative = _safe_relative(
        raw.get("xdg_cache_home"),
        "umu.offline_environment.xdg_cache_home",
    )

    xdg_data = _path_under(root, xdg_data_relative)
    xdg_cache = _path_under(root, xdg_cache_relative)

    for label, path in (
        ("XDG data", xdg_data),
        ("XDG cache", xdg_cache),
    ):
        if path.is_symlink() or not path.is_dir():
            raise UmuAdapterError(
                f"{label} directory is absent or linked."
            )

    runtime_update = raw.get("runtime_update")
    if runtime_update is not False:
        raise UmuAdapterError(
            "Offline UMU requires runtime_update=false."
        )

    runtime_raw = raw.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise UmuAdapterError(
            "umu.offline_environment.runtime must be an object."
        )
    if set(runtime_raw) != {
        "family",
        "version",
        "path",
        "required_paths",
    }:
        raise UmuAdapterError(
            "umu.offline_environment.runtime keys are incomplete or unknown."
        )

    family = runtime_raw.get("family")
    version = runtime_raw.get("version")
    if (
        not isinstance(family, str)
        or not family
        or not isinstance(version, str)
        or not version
    ):
        raise UmuAdapterError(
            "Offline runtime family/version is invalid."
        )

    runtime_relative = _safe_relative(
        runtime_raw.get("path"),
        "umu.offline_environment.runtime.path",
    )
    runtime_root = _path_under(xdg_data, runtime_relative)
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise UmuAdapterError(
            "Offline runtime directory is absent or linked."
        )

    required_paths_raw = runtime_raw.get("required_paths")
    if not isinstance(required_paths_raw, list) or not required_paths_raw:
        raise UmuAdapterError(
            "Offline runtime required_paths must be a non-empty array."
        )
    required_paths = []
    for index, item in enumerate(required_paths_raw):
        if not isinstance(item, dict) or set(item) != {"path", "type"}:
            raise UmuAdapterError(
                "Offline runtime required path declaration is invalid."
            )
        required_paths.append(
            _verify_required_runtime_path(
                runtime_root,
                item,
                index=index,
            )
        )

    cache_entries_raw = raw.get("cache_entries")
    if not isinstance(cache_entries_raw, list):
        raise UmuAdapterError(
            "Offline cache_entries must be an array."
        )

    cache_entries: list[dict[str, Any]] = []
    seen_cache_ids: set[str] = set()
    seen_cache_paths: set[str] = set()

    for index, item in enumerate(cache_entries_raw):
        if not isinstance(item, dict) or set(item) != {
            "id",
            "path",
            "required_files",
        }:
            raise UmuAdapterError(
                f"Offline cache entry {index} is invalid."
            )

        cache_id = item.get("id")
        if (
            not isinstance(cache_id, str)
            or not cache_id
            or cache_id in seen_cache_ids
        ):
            raise UmuAdapterError(
                f"Offline cache entry {index} has an invalid or duplicate ID."
            )
        seen_cache_ids.add(cache_id)

        relative = _safe_relative(
            item.get("path"),
            f"umu.offline_environment.cache_entries[{index}].path",
        )
        relative_string = relative.as_posix()
        if relative_string in seen_cache_paths:
            raise UmuAdapterError(
                f"Duplicate offline cache path: {relative_string}"
            )
        seen_cache_paths.add(relative_string)

        cache_root = _path_under(xdg_cache, relative)
        if cache_root.is_symlink() or not cache_root.is_dir():
            raise UmuAdapterError(
                f"Offline cache directory is absent or linked: {relative}"
            )

        files_raw = item.get("required_files")
        if not isinstance(files_raw, list):
            raise UmuAdapterError(
                f"Offline cache entry {cache_id!r} required_files is invalid."
            )

        files: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        for file_index, file_item in enumerate(files_raw):
            if not isinstance(file_item, dict) or set(file_item) != {
                "path",
                "digest",
                "size",
            }:
                raise UmuAdapterError(
                    f"Offline cache file {cache_id}:{file_index} is invalid."
                )
            file_relative = _safe_relative(
                file_item.get("path"),
                f"offline cache {cache_id} required file",
            )
            file_name = file_relative.as_posix()
            if file_name in seen_files:
                raise UmuAdapterError(
                    f"Duplicate offline cache file: {cache_id}:{file_name}"
                )
            seen_files.add(file_name)

            expected_digest = _parse_digest(
                file_item.get("digest"),
                f"offline cache {cache_id}:{file_name}.digest",
            )
            expected_size = file_item.get("size")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise UmuAdapterError(
                    f"Offline cache file size is invalid: {cache_id}:{file_name}"
                )

            file_path = _path_under(cache_root, file_relative)
            _verify_regular_file(
                file_path,
                expected_digest,
                expected_size=expected_size,
                label=f"Offline cache file {cache_id}:{file_name}",
            )
            files.append(
                {
                    "path": file_name,
                    "digest": f"sha256:{expected_digest}",
                    "size": expected_size,
                }
            )

        cache_entries.append(
            {
                "id": cache_id,
                "path": relative_string,
                "required_files": files,
            }
        )

    return {
        "xdg_data_home": xdg_data_relative.as_posix(),
        "xdg_cache_home": xdg_cache_relative.as_posix(),
        "runtime_update": False,
        "runtime": {
            "family": family,
            "version": version,
            "path": runtime_relative.as_posix(),
            "required_paths": required_paths,
        },
        "cache_entries": cache_entries,
    }


def _offline_environment_variables(
    root: Path,
    receipt: dict[str, Any],
) -> dict[str, str]:
    offline = receipt.get("offline_environment")
    verified = _verify_offline_environment(root, offline)
    if verified is None:
        return {}

    xdg_data = _path_under(
        root,
        _safe_relative(
            verified["xdg_data_home"],
            "receipt.offline_environment.xdg_data_home",
        ),
    )
    xdg_cache = _path_under(
        root,
        _safe_relative(
            verified["xdg_cache_home"],
            "receipt.offline_environment.xdg_cache_home",
        ),
    )

    return {
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CACHE_HOME": str(xdg_cache),
        "UMU_RUNTIME_UPDATE": "0",
    }

def _manifest_check_helper_source() -> str:
    from . import portable_manifest_check

    path = Path(portable_manifest_check.__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise UmuAdapterError(
            "Cannot locate the shared manifest-check helper source."
        )
    return path.read_text(encoding="utf-8")


def _portable_umu_runtime_source() -> str:
    from . import portable_umu_runtime

    path = Path(portable_umu_runtime.__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise UmuAdapterError("Cannot locate the portable UMU runtime source.")
    return path.read_text(encoding="utf-8")


def _operational_script(command: str) -> str:
    if command not in {"play", "verify", "uninstall"}:
        raise UmuAdapterError("Unsupported operational UMU command.")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd -P)"
exec "${{PYTHON:-python3}}" \
  "$root/{PORTABLE_RUNTIME_DESTINATION}" \
  {command} --root "$root" "$@"
"""


def _write_operational_file(path: Path, payload: str, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise UmuAdapterError(
            f"Operational path already exists: {path.relative_to(path.parents[1])}"
        )
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(mode)


def _install_operational_scripts(staging: Path) -> dict[str, str]:
    runtime = staging / PORTABLE_RUNTIME_DESTINATION
    launcher = staging / ROOT_LAUNCHER
    verifier = staging / ROOT_VERIFIER
    uninstaller = staging / ROOT_UNINSTALLER
    _write_operational_file(runtime, _portable_umu_runtime_source(), 0o600)
    compile(runtime.read_text(encoding="utf-8"), str(runtime), "exec")
    # Fase 5: helper compartido para VERIFICAR.sh (manifest-based check).
    _manifest_check_target = staging / "metadata/ogv_manifest_check.py"
    _write_operational_file(
        _manifest_check_target, _manifest_check_helper_source(), 0o600
    )
    compile(
        _manifest_check_target.read_text(encoding="utf-8"),
        str(_manifest_check_target),
        "exec",
    )
    _write_operational_file(launcher, _operational_script("play"), 0o700)
    _write_operational_file(verifier, _operational_script("verify"), 0o700)
    _write_operational_file(
        uninstaller, _operational_script("uninstall"), 0o700
    )
    if shutil.which("bash") is not None:
        for script in (launcher, verifier, uninstaller):
            process = subprocess.run(
                ["bash", "-n", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if (
                isinstance(process.returncode, int)
                and process.returncode != 0
            ):
                raise UmuAdapterError(
                    f"Generated shell script is invalid: {script.name}"
                )
    return {
        "launcher": ROOT_LAUNCHER,
        "verifier": ROOT_VERIFIER,
        "uninstaller": ROOT_UNINSTALLER,
        "portable_runtime": PORTABLE_RUNTIME_DESTINATION,
    }


def _discover_offline_environment(root: Path) -> dict[str, Any]:
    """Derive and validate one complete preserved UMU Steam runtime."""

    xdg_data_relative = PurePosixPath("engine/xdg-data")
    xdg_cache_relative = PurePosixPath("engine/xdg-cache")
    xdg_data = _path_under(root, xdg_data_relative)
    xdg_cache = _path_under(root, xdg_cache_relative)
    if xdg_data.is_symlink() or not xdg_data.is_dir():
        raise UmuAdapterError("Preserved UMU XDG data directory is absent.")
    if xdg_cache.exists() or xdg_cache.is_symlink():
        if xdg_cache.is_symlink() or not xdg_cache.is_dir():
            raise UmuAdapterError("Preserved UMU XDG cache path is invalid.")
    else:
        xdg_cache.mkdir(parents=True, mode=0o700)

    umu_root = xdg_data / "umu"
    if umu_root.is_symlink() or not umu_root.is_dir():
        raise UmuAdapterError(
            "Preserved UMU runtime root engine/xdg-data/umu is absent."
        )
    candidates = sorted(
        path
        for path in umu_root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and re.fullmatch(r"steamrt[0-9]+", path.name)
    )
    if len(candidates) != 1:
        raise UmuAdapterError(
            "Expected exactly one preserved steamrtN runtime under "
            "engine/xdg-data/umu."
        )
    runtime_root = candidates[0]
    family = runtime_root.name
    platform_prefix = _runtime_platform_prefix(family)
    platform_directories = sorted(
        path
        for path in runtime_root.glob(f"{platform_prefix}_platform_*")
        if path.is_dir() and not path.is_symlink()
    )
    if len(platform_directories) != 1:
        raise UmuAdapterError(
            f"Preserved {family} is incomplete: expected exactly one "
            f"{platform_prefix}_platform_* directory."
        )
    required = [
        {"path": "VERSIONS.txt", "type": "file"},
        {"path": "_v2-entry-point", "type": "file"},
        {"path": "pressure-vessel", "type": "directory"},
        {
            "path": platform_directories[0].name,
            "type": "directory",
        },
    ]
    for index, item in enumerate(required):
        _verify_required_runtime_path(runtime_root, item, index=index)
    entrypoint = runtime_root / "_v2-entry-point"
    if not os.access(entrypoint, os.X_OK):
        raise UmuAdapterError(
            f"Preserved {family} _v2-entry-point is not executable."
        )

    return {
        "xdg_data_home": xdg_data_relative.as_posix(),
        "xdg_cache_home": xdg_cache_relative.as_posix(),
        "runtime_update": False,
        "runtime": {
            "family": family,
            "version": platform_directories[0].name.removeprefix(
                f"{platform_prefix}_platform_"
            ),
            "path": f"umu/{family}",
            "required_paths": required,
        },
        "cache_entries": [],
    }


def materialize_umu_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
    destination: Path,
    state_root: Path | None = None,
    save_id: str | None = None,
    state_backup: Path | None = None,
    require_state_backup: bool = False,
    state_capsule_path: Path | None = None,
) -> UmuMaterializationResult:
    """Verify, assemble, and atomically publish one UMU materialization."""

    capsule_path = capsule_path.expanduser().absolute()
    state_capsule_path = (
        capsule_path
        if state_capsule_path is None
        else state_capsule_path.expanduser().absolute()
    )
    capsule_root = capsule_path.parent.resolve()
    vault_root = vault_root.expanduser().resolve()
    destination = _canonical_destination(destination)
    state_root = (
        state_root.expanduser().resolve() if state_root is not None else None
    )

    if state_backup is not None and (
        state_root is not None or save_id is not None
    ):
        raise UmuAdapterError(
            "Generic --state-backup cannot be combined with "
            "legacy UMU state_root/save_id selection."
        )

    capsule, profile, contract = _profile_contract(
        capsule_path, profile_id
    )
    capsule_id = capsule["capsule_id"]

    state_capsule = _load_json(
        state_capsule_path,
        "State capsule",
    )
    if state_capsule.get("capsule_id") != capsule_id:
        raise UmuAdapterError(
            "State capsule belongs to another capsule."
        )
    try:
        state_selection = prepare_composition_state(
            capsule_path=state_capsule_path,
            state_backup=state_backup,
            require_declared_state=require_state_backup,
        )
    except CompositionStateError as exc:
        raise UmuAdapterError(str(exc)) from exc

    persistent_state_raw = state_capsule.get(
        "persistent_state",
        [],
    )
    if (
        not isinstance(persistent_state_raw, list)
        or any(
            not isinstance(item, dict)
            for item in persistent_state_raw
        )
    ):
        raise UmuAdapterError(
            "capsule.persistent_state must be an array of objects."
        )

    persistent_state_receipts: list[dict[str, Any]] = []
    for index, item in enumerate(persistent_state_raw):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise UmuAdapterError(
                f"persistent_state[{index}].id is invalid."
            )
        item_path = _safe_relative(
            item.get("path"),
            f"persistent_state[{index}].path",
        )
        persistent_state_receipts.append(
            {
                "id": item_id,
                "path": item_path.as_posix(),
                "preserve_on_remove": (
                    item.get("backup", True) is True
                ),
                "sensitive": item.get("sensitive", False) is True,
            }
        )

    dependencies = profile.get("dependencies")
    layout = contract.get("layout")
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) or not item for item in dependencies
    ):
        raise UmuAdapterError("profile.dependencies must contain object IDs.")
    if not isinstance(layout, list) or not layout:
        raise UmuAdapterError("umu.layout must be a non-empty array.")

    archive_policies = _layout_archive_policies(layout)

    mapped_ids = [
        item.get("object")
        for item in layout
        if isinstance(item, dict)
    ]
    if set(mapped_ids) != set(dependencies):
        raise UmuAdapterError(
            "umu.layout must map every profile dependency."
        )

    allowed_absolute = _allowed_absolute_links(contract)

    legacy_state_archives = contract.get("state_archives", [])
    if state_selection is not None and legacy_state_archives:
        raise UmuAdapterError(
            "Generic persistent-state backups cannot be combined "
            "with legacy umu.state_archives."
        )

    generic_prefix_relative = None
    if state_selection is not None:
        generic_paths = contract.get("paths")
        if not isinstance(generic_paths, dict):
            raise UmuAdapterError("umu.paths must be an object.")

        generic_prefix_relative = _safe_relative(
            generic_paths.get("prefix"),
            "umu.paths.prefix",
        )

    state_archives, selected_save = _state_archive(
        contract=contract,
        state_root=state_root,
        save_id=save_id,
    )

    if destination.exists() or destination.is_symlink():
        try:
            verification = verify_umu_materialization(
                destination=destination
            )
        except UmuAdapterError as exc:
            raise UmuAdapterError(
                "Destination exists and is not a valid UMU materialization."
            ) from exc
        if verification.capsule_id != capsule_id or verification.profile_id != profile_id:
            raise UmuAdapterError(
                "Existing UMU materialization belongs to another profile."
            )
        receipt = _load_receipt(destination)

        expected_backup_id = (
            state_selection.backup_id
            if state_selection is not None
            else None
        )
        receipt_state = receipt.get("state_restore")
        if (
            receipt_state is not None
            and not isinstance(receipt_state, dict)
        ):
            raise UmuAdapterError(
                "Existing UMU state_restore receipt is invalid."
            )
        actual_backup_id = (
            receipt_state.get("backup_id")
            if isinstance(receipt_state, dict)
            else None
        )
        if actual_backup_id != expected_backup_id:
            raise UmuAdapterError(
                "Existing UMU materialization uses another "
                "persistent-state baseline."
            )

        if receipt.get("selected_save") != selected_save:
            raise UmuAdapterError(
                "Existing UMU materialization uses another save selection."
            )
        return UmuMaterializationResult(
            schema=0,
            receipt_id=receipt["receipt_id"],
            capsule_id=capsule_id,
            profile_id=profile_id,
            backend="umu",
            destination=str(destination),
            object_count=len(dependencies),
            selected_save=selected_save,
            complete=True,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock, lock_descriptor = _acquire_lock(destination)
    staging = destination.parent / (
        f".ogv-umu-{destination.name}-{os.getpid()}-"
        f"{secrets.token_hex(8)}"
    )
    promoted = False
    state_snapshot = destination.parent / (
        f".ogv-umu-state-snapshot-{destination.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    state_evidence = None

    object_receipts: list[dict[str, Any]] = []
    launcher_receipts: list[dict[str, Any]] = []
    protected_receipts: list[dict[str, Any]] = []
    symlink_receipts: list[dict[str, Any]] = []
    hardlink_receipts: list[dict[str, Any]] = []
    state_receipts: list[dict[str, Any]] = []

    try:
        staging.mkdir(mode=0o700)
        extraction_root = staging / ".object-extraction"
        extraction_root.mkdir(mode=0o700)

        objects = capsule.get("objects")
        if not isinstance(objects, list):
            raise UmuAdapterError("capsule.objects must be an array.")
        object_declarations: dict[str, dict[str, Any]] = {}
        for item in objects:
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    "Every capsule object declaration must be an object."
                )
            object_id_value = item.get("id")
            if not isinstance(object_id_value, str) or not object_id_value:
                raise UmuAdapterError(
                    "Every capsule object requires a non-empty ID."
                )
            if object_id_value in object_declarations:
                raise UmuAdapterError(
                    f"Duplicate capsule object ID: {object_id_value}"
                )
            object_declarations[object_id_value] = item

        object_roots: dict[str, Path] = {}
        for object_id in dependencies:
            try:
                spec = resolve_capsule_object(
                    capsule_path=capsule_path,
                    object_id=object_id,
                    vault_root=vault_root,
                )
                verification = verify_object(spec)
            except VerifyError as exc:
                raise UmuAdapterError(str(exc)) from exc
            if not verification.verified:
                raise UmuAdapterError(
                    f"Immutable object failed verification: {object_id}"
                )
            object_root = extraction_root / object_id
            object_root.mkdir(mode=0o700)
            declaration = object_declarations.get(object_id)
            if declaration is None:
                raise UmuAdapterError(
                    f"Missing capsule object declaration: {object_id}"
                )
            declared_format = declaration.get("format")
            if not isinstance(declared_format, str):
                raise UmuAdapterError(
                    f"Object {object_id} has no declared archive format."
                )
            policy = archive_policies.get(
                object_id,
                {
                    "allow_absolute_symlinks": False,
                    "allow_hardlinks": False,
                },
            )
            members = _extract_archive(
                spec.path,
                object_root,
                allowed_absolute=allowed_absolute,
                allow_absolute_symlinks=policy[
                    "allow_absolute_symlinks"
                ],
                allow_hardlinks=policy["allow_hardlinks"],
                declared_format=declared_format,
            )
            object_roots[object_id] = object_root
            object_receipts.append(
                {
                    "id": object_id,
                    "digest": spec.expected_digest,
                    "size": verification.actual_size,
                    "member_count": len(members),
                }
            )

        for index, mapping in enumerate(layout):
            if not isinstance(mapping, dict):
                raise UmuAdapterError(
                    f"umu.layout[{index}] must be an object."
                )
            object_id = mapping.get("object")
            if object_id not in object_roots:
                raise UmuAdapterError(
                    f"umu.layout[{index}] references an unknown object."
                )
            _merge_mapping(
                object_root=object_roots[object_id],
                staging_root=staging,
                source_value=mapping.get("source"),
                destination_value=mapping.get("destination"),
                field=f"umu.layout[{index}]",
            )

        shutil.rmtree(extraction_root)

        nested = contract.get("nested_archives", [])
        if not isinstance(nested, list):
            raise UmuAdapterError("umu.nested_archives must be an array.")
        for index, item in enumerate(nested):
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    f"umu.nested_archives[{index}] must be an object."
                )
            relative = _safe_relative(
                item.get("path"),
                f"umu.nested_archives[{index}].path",
            )
            archive_path = _path_under(staging, relative)
            expected = _parse_digest(
                item.get("digest"),
                f"umu.nested_archives[{index}].digest",
            )
            _verify_regular_file(
                archive_path,
                expected,
                label=f"Nested archive {relative}",
            )
            destination_relative = _safe_relative(
                item.get("destination", "."),
                f"umu.nested_archives[{index}].destination",
                dot=True,
            )
            nested_destination = _path_under(staging, destination_relative)
            _extract_archive(
                archive_path,
                nested_destination,
                allowed_absolute=allowed_absolute,
            )
            if item.get("remove_after", True) is True:
                archive_path.unlink()
                parent = archive_path.parent
                while parent != staging and parent.name:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

        for item, archive_path in state_archives:
            expected = _parse_digest(
                item.get("digest"),
                f"state archive {item.get('id')}.digest",
            )
            _verify_regular_file(
                archive_path,
                expected,
                label=f"State archive {item.get('id')}",
            )
            _extract_archive(
                archive_path,
                staging,
                allowed_absolute=allowed_absolute,
            )
            state_receipts.append(
                {
                    "id": item["id"],
                    "policy": item["policy"],
                    "digest": f"sha256:{expected}",
                    "filename": item["filename"],
                }
            )

        launchers = contract.get("launchers")
        if not isinstance(launchers, list) or not launchers:
            raise UmuAdapterError("umu.launchers must be a non-empty array.")
        for index, item in enumerate(launchers):
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    f"umu.launchers[{index}] must be an object."
                )
            launcher_receipts.append(
                _copy_capsule_asset(
                    capsule_root=capsule_root,
                    staging_root=staging,
                    item=item,
                    context=f"umu.launchers[{index}]",
                )
            )

        metadata_root = staging / "metadata/verification"
        metadata_root.mkdir(parents=True)

        protected = contract.get("protected_manifests", [])
        if not isinstance(protected, list):
            raise UmuAdapterError(
                "umu.protected_manifests must be an array."
            )
        for index, item in enumerate(protected):
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    f"umu.protected_manifests[{index}] must be an object."
                )
            when_save = item.get("when_save")
            if when_save is not None:
                if not isinstance(when_save, str) or not when_save:
                    raise UmuAdapterError(
                        f"umu.protected_manifests[{index}].when_save "
                        "must be a non-empty string."
                    )
                if selected_save != when_save:
                    continue
            copied, digest = _copy_verification_asset(
                capsule_root=capsule_root,
                metadata_root=metadata_root,
                source_value=item.get("source"),
                context=f"umu.protected_manifests[{index}].source",
            )
            protected_receipts.append(
                {
                    "name": copied.name,
                    "digest": digest,
                }
            )

        symlink_manifests = contract.get("symlink_manifests", [])
        if not isinstance(symlink_manifests, list):
            raise UmuAdapterError(
                "umu.symlink_manifests must be an array."
            )
        for index, item in enumerate(symlink_manifests):
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    f"umu.symlink_manifests[{index}] must be an object."
                )
            prefixes_raw = item.get("prefixes")
            if not isinstance(prefixes_raw, list) or not prefixes_raw:
                raise UmuAdapterError(
                    f"umu.symlink_manifests[{index}].prefixes is invalid."
                )
            prefixes = [
                _safe_relative(
                    value,
                    f"umu.symlink_manifests[{index}].prefixes",
                ).as_posix()
                for value in prefixes_raw
            ]
            copied, digest = _copy_verification_asset(
                capsule_root=capsule_root,
                metadata_root=metadata_root,
                source_value=item.get("source"),
                context=f"umu.symlink_manifests[{index}].source",
            )
            allow_unresolved = item.get("allow_unresolved", False)
            if not isinstance(allow_unresolved, bool):
                raise UmuAdapterError(
                    f"umu.symlink_manifests[{index}].allow_unresolved "
                    "must be boolean."
                )
            symlink_receipts.append(
                {
                    "name": copied.name,
                    "digest": digest,
                    "prefixes": prefixes,
                    "allow_unresolved": allow_unresolved,
                }
            )

        hardlink_manifests = contract.get("hardlink_manifests", [])
        if not isinstance(hardlink_manifests, list):
            raise UmuAdapterError(
                "umu.hardlink_manifests must be an array."
            )
        for index, item in enumerate(hardlink_manifests):
            if not isinstance(item, dict):
                raise UmuAdapterError(
                    f"umu.hardlink_manifests[{index}] must be an object."
                )
            root_relative = _safe_relative(
                item.get("root"),
                f"umu.hardlink_manifests[{index}].root",
            )
            copied, digest = _copy_verification_asset(
                capsule_root=capsule_root,
                metadata_root=metadata_root,
                source_value=item.get("source"),
                context=f"umu.hardlink_manifests[{index}].source",
            )
            hardlink_receipts.append(
                {
                    "name": copied.name,
                    "digest": digest,
                    "root": root_relative.as_posix(),
                }
            )

        sanitizer_path = _safe_relative(
            contract.get("paths", {}).get("sanitizer"),
            "umu.paths.sanitizer",
        )
        sanitizer = _path_under(staging, sanitizer_path)
        if sanitizer.is_symlink() or not os.access(sanitizer, os.X_OK):
            raise UmuAdapterError("The UMU sanitizer is absent or not executable.")
        sanitizer_result = subprocess.run(
            [str(sanitizer)],
            cwd=staging,
            check=False,
        )
        if sanitizer_result.returncode != 0:
            raise UmuAdapterError(
                f"The UMU sanitizer failed: {sanitizer_result.returncode}"
            )

        if state_selection is not None:
            assert generic_prefix_relative is not None
            generic_prefix = _path_under(
                staging,
                generic_prefix_relative,
            )
            if (
                generic_prefix.is_symlink()
                or not generic_prefix.is_dir()
            ):
                raise UmuAdapterError(
                    "The declared UMU prefix root is absent or linked."
                )

            try:
                state_evidence = restore_composition_state(
                    capsule_path=state_capsule_path,
                    state_root=generic_prefix,
                    state_root_relative=(
                        generic_prefix_relative.as_posix()
                    ),
                    selection=state_selection,
                    snapshot=state_snapshot,
                    evidence_root=staging / "receipts/state",
                    evidence_relative="receipts/state",
                )
            except CompositionStateError as exc:
                raise UmuAdapterError(str(exc)) from exc

        mutable_raw = contract.get("mutable_paths", [])
        if not isinstance(mutable_raw, list):
            raise UmuAdapterError("umu.mutable_paths must be an array.")
        mutable_paths = sorted(
            {
                _safe_relative(value, "umu.mutable_paths").as_posix()
                for value in mutable_raw
            }
        )

        _verify_manifest_set(
            staging,
            protected_receipts,
            skip_paths=set(),
        )
        symlink_count = _verify_symlink_sets(
            staging,
            symlink_receipts,
        )
        hardlink_group_count = _verify_hardlink_sets(
            staging,
            hardlink_receipts,
        )
        paths_for_runtime_context = contract.get("paths")
        if not isinstance(paths_for_runtime_context, dict):
            raise UmuAdapterError("umu.paths must be an object.")
        runtime_var_for_context = _safe_relative(
            paths_for_runtime_context.get("runtime_var"),
            "umu.paths.runtime_var",
        )
        runtime_context_prefixes = (
            _runtime_context_unresolved_prefixes(
                staging,
                runtime_var_for_context,
            )
        )
        unresolved_prefixes = {
            prefix
            for item in symlink_receipts
            if item.get("allow_unresolved") is True
            for prefix in item["prefixes"]
        }
        unresolved_prefixes.update(
            runtime_context_prefixes
        )
        _verify_no_broken_symlinks(
            staging,
            allowed_unresolved_prefixes=unresolved_prefixes,
        )

        offline_contract = contract.get("offline_environment")
        if offline_contract is None:
            offline_contract = _discover_offline_environment(staging)
        offline_environment = _verify_offline_environment(
            staging,
            offline_contract,
        )
        if offline_environment is None:
            raise UmuAdapterError(
                "UMU materialization has no verified offline runtime."
            )

        operational_paths = _install_operational_scripts(staging)

        paths = contract.get("paths")
        if not isinstance(paths, dict):
            raise UmuAdapterError("umu.paths must be an object.")
        launcher_relative = _safe_relative(
            paths.get("launcher"), "umu.paths.launcher"
        )
        runtime_var_relative = _safe_relative(
            paths.get("runtime_var"), "umu.paths.runtime_var"
        )
        runtime_var = _path_under(staging, runtime_var_relative)
        if (
            not runtime_var.is_dir()
            or runtime_var.is_symlink()
        ):
            raise UmuAdapterError(
                "The declared runtime var directory is absent or linked."
            )

        receipt_paths = {
            "launcher": launcher_relative.as_posix(),
            "sanitizer": sanitizer_path.as_posix(),
            "runtime_var": runtime_var_relative.as_posix(),
        }
        if generic_prefix_relative is not None:
            receipt_paths["prefix"] = (
                generic_prefix_relative.as_posix()
            )

        receipt_id = f"umu-materialization-{uuid.uuid4()}"
        receipt = {
            "schema": 0,
            "receipt_id": receipt_id,
            "capsule_id": capsule_id,
            "profile_id": profile_id,
            "backend": "umu",
            "created_at": _now(),
            "orchestrator_version": __version__,
            "destination": ".",
            "objects": object_receipts,
            "state_archives": state_receipts,
            "selected_save": selected_save,
            "persistent_state": persistent_state_receipts,
            "launchers": launcher_receipts,
            "protected_manifests": protected_receipts,
            "symlink_manifests": symlink_receipts,
            "hardlink_manifests": hardlink_receipts,
            "offline_environment": offline_environment,
            "operational_paths": operational_paths,
            "mutable_paths": mutable_paths,
            "paths": receipt_paths,
            "network": profile.get("launch", {}).get(
                "network", "host_default"
            ),
            "initial_verification": {
                "protected_file_count": _verify_manifest_set(
                    staging,
                    protected_receipts,
                    skip_paths=set(),
                ),
                "symlink_count": symlink_count,
                "hardlink_group_count": hardlink_group_count,
                "broken_symlinks": 0,
                "broken_symlinks_outside_allowed_prefixes": 0,
                "allowed_unresolved_symlink_prefixes": sorted(
                    unresolved_prefixes
                ),
                "runtime_var_preserved": True,
                "offline_environment_verified":
                    offline_environment is not None,
            },
            "complete": True,
        }
        if state_evidence is not None:
            receipt["state_restore"] = state_evidence.to_dict()

        _write_json_atomic(staging / RECEIPT_NAME, receipt)

        _rename_noreplace(staging, destination)
        promoted = True
        return UmuMaterializationResult(
            schema=0,
            receipt_id=receipt_id,
            capsule_id=capsule_id,
            profile_id=profile_id,
            backend="umu",
            destination=str(destination),
            object_count=len(object_receipts),
            selected_save=selected_save,
            complete=True,
        )
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        if isinstance(exc, UmuAdapterError):
            raise
        raise UmuAdapterError(str(exc)) from exc
    finally:
        if not promoted and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if state_snapshot.exists():
            shutil.rmtree(state_snapshot, ignore_errors=True)
        _release_lock(lock, lock_descriptor)


def verify_umu_materialization(
    *,
    destination: Path,
) -> UmuVerificationResult:
    """Verify one published UMU materialization after possible gameplay."""

    destination = _canonical_destination(destination)
    if destination.is_symlink() or not destination.is_dir():
        raise UmuAdapterError(
            "UMU materialization must be a regular directory."
        )
    receipt = _load_receipt(destination)
    try:
        verify_composition_state_evidence(
            root=destination,
            evidence=receipt.get("state_restore"),
            capsule_id=receipt["capsule_id"],
        )
    except CompositionStateError as exc:
        raise UmuAdapterError(
            f"Invalid state restoration evidence: {exc}"
        ) from exc

    launchers = receipt.get("launchers")
    protected = receipt.get("protected_manifests")
    symlink_manifests = receipt.get("symlink_manifests")
    hardlink_manifests = receipt.get("hardlink_manifests", [])
    mutable_paths = receipt.get("mutable_paths")
    persistent_state = receipt.get("persistent_state", [])
    paths = receipt.get("paths")
    operational_paths = receipt.get("operational_paths")
    if (
        not isinstance(launchers, list)
        or not isinstance(protected, list)
        or not isinstance(symlink_manifests, list)
        or not isinstance(hardlink_manifests, list)
        or not isinstance(mutable_paths, list)
        or not isinstance(persistent_state, list)
        or not isinstance(paths, dict)
        or not isinstance(operational_paths, dict)
    ):
        raise UmuAdapterError("UMU receipt is incomplete.")

    for index, item in enumerate(persistent_state):
        if not isinstance(item, dict):
            raise UmuAdapterError(
                "UMU persistent-state receipt is invalid."
            )
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise UmuAdapterError(
                f"receipt.persistent_state[{index}].id is invalid."
            )
        _safe_relative(
            item.get("path"),
            f"receipt.persistent_state[{index}].path",
        )
        if not isinstance(item.get("preserve_on_remove"), bool):
            raise UmuAdapterError(
                f"receipt.persistent_state[{index}]."
                "preserve_on_remove must be boolean."
            )
        if not isinstance(item.get("sensitive"), bool):
            raise UmuAdapterError(
                f"receipt.persistent_state[{index}]."
                "sensitive must be boolean."
            )

    for item in launchers:
        if not isinstance(item, dict):
            raise UmuAdapterError("UMU launcher receipt is invalid.")
        relative = _safe_relative(item.get("path"), "receipt.launcher.path")
        expected = _parse_digest(
            item.get("digest"), "receipt.launcher.digest"
        )
        path = _path_under(destination, relative)
        _verify_regular_file(path, expected, label=f"Launcher {relative}")
        mode = item.get("mode")
        if isinstance(mode, int) and stat.S_IMODE(path.stat().st_mode) != mode:
            raise UmuAdapterError(f"Launcher mode changed: {relative}")

    protected_count = _verify_manifest_set(
        destination,
        protected,
        skip_paths=set(mutable_paths),
    )
    symlink_count = _verify_symlink_sets(
        destination,
        symlink_manifests,
    )
    hardlink_group_count = _verify_hardlink_sets(
        destination,
        hardlink_manifests,
    )
    runtime_var_for_context = _safe_relative(
        paths.get("runtime_var"),
        "receipt.paths.runtime_var",
    )
    runtime_context_prefixes = (
        _runtime_context_unresolved_prefixes(
            destination,
            runtime_var_for_context,
        )
    )
    unresolved_prefixes = {
        prefix
        for item in symlink_manifests
        if item.get("allow_unresolved") is True
        for prefix in item["prefixes"]
    }
    unresolved_prefixes.update(
        runtime_context_prefixes
    )
    _verify_no_broken_symlinks(
        destination,
        allowed_unresolved_prefixes=unresolved_prefixes,
    )
    _verify_offline_environment(
        destination,
        receipt.get("offline_environment"),
    )

    for key in ("launcher", "verifier", "uninstaller", "portable_runtime"):
        relative = _safe_relative(
            operational_paths.get(key),
            f"receipt.operational_paths.{key}",
        )
        path = _path_under(destination, relative)
        if path.is_symlink() or not path.is_file():
            raise UmuAdapterError(
                f"UMU operational file is absent: {key}"
            )
        if key != "portable_runtime" and not os.access(path, os.X_OK):
            raise UmuAdapterError(
                f"UMU operational script is not executable: {key}"
            )

    runtime_var_relative = _safe_relative(
        paths.get("runtime_var"), "receipt.paths.runtime_var"
    )
    runtime_var = _path_under(destination, runtime_var_relative)
    if (
        not runtime_var.is_dir()
        or runtime_var.is_symlink()
    ):
        raise UmuAdapterError(
            "Runtime var is absent or linked."
        )

    return UmuVerificationResult(
        schema=0,
        capsule_id=receipt["capsule_id"],
        profile_id=receipt["profile_id"],
        backend="umu",
        destination=str(destination),
        protected_file_count=protected_count,
        symlink_count=symlink_count,
        hardlink_group_count=hardlink_group_count,
        verified=True,
    )


def run_umu_materialization(
    *,
    destination: Path,
    arguments: Sequence[str] = (),
) -> UmuRunResult:
    """Run through the generated JUGAR.sh contract with network isolation."""

    from .portable_umu_runtime import PortableUmuError, play as portable_play

    destination = _canonical_destination(destination)
    try:
        document = portable_play(destination, arguments=arguments)
    except PortableUmuError as exc:
        raise UmuAdapterError(str(exc)) from exc
    return UmuRunResult(
        schema=0,
        capsule_id=str(document["capsule_id"]),
        profile_id=str(document["profile_id"]),
        backend="umu",
        destination=str(destination),
        process_rc=int(document["process_rc"]),
        duration_ms=int(document["duration_ms"]),
        sanitizer_rc=int(document["sanitizer_rc"]),
        verified_after_run=bool(document["verified_after_run"]),
        complete=bool(document["complete"]),
    )


def remove_umu_materialization(
    *,
    destination: Path,
    confirm_state_preserved: bool,
) -> UmuRemovalResult:
    """Remove a recognized UMU derivative after explicit state confirmation."""

    destination = _canonical_destination(destination)
    verification = verify_umu_materialization(destination=destination)
    receipt = _load_receipt(destination)
    selected_save = receipt.get("selected_save")
    state_archives = receipt.get("state_archives", [])
    persistent_state = receipt.get("persistent_state", [])
    if not isinstance(persistent_state, list):
        raise UmuAdapterError(
            "UMU persistent-state receipt is invalid."
        )
    has_persistent_state = bool(state_archives) or any(
        isinstance(item, dict)
        and item.get("preserve_on_remove") is True
        for item in persistent_state
    )
    if has_persistent_state and not confirm_state_preserved:
        raise UmuAdapterError(
            "UMU persistent state must be preserved before removal. "
            "Re-run with --confirm-state-preserved only after backup."
        )

    lock, lock_descriptor = _acquire_lock(destination)
    detached = destination.parent / (
        f".ogv-umu-remove-{destination.name}-{uuid.uuid4()}"
    )
    try:
        os.replace(destination, detached)
        shutil.rmtree(detached)
    except OSError as exc:
        if detached.exists() and not destination.exists():
            try:
                os.replace(detached, destination)
            except OSError:
                pass
        raise UmuAdapterError(f"Cannot remove UMU materialization: {exc}") from exc
    finally:
        _release_lock(lock, lock_descriptor)

    return UmuRemovalResult(
        schema=0,
        capsule_id=verification.capsule_id,
        profile_id=verification.profile_id,
        backend="umu",
        destination=str(destination),
        selected_save=selected_save if isinstance(selected_save, str) else None,
        state_preservation_confirmed=confirm_state_preserved,
        removed=True,
        complete=True,
    )
