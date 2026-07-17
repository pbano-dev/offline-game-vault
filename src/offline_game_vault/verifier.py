"""Streaming verification of immutable vault objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


class VerifyError(Exception):
    """Raised when an object cannot be verified safely."""


@dataclass(frozen=True)
class VerificationResult:
    schema: int
    object_id: str | None
    path: str
    algorithm: str
    expected_digest: str
    actual_digest: str
    digest_match: bool
    expected_size: int | None
    actual_size: int
    size_match: bool | None
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObjectSpec:
    object_id: str | None
    path: Path
    expected_digest: str
    expected_size: int | None
    vault_root: Path | None = None


def _parse_digest(value: str) -> str:
    match = _DIGEST_RE.fullmatch(value)
    if not match:
        raise VerifyError(
            "Digest must use lowercase form "
            "'sha256:<64 hexadecimal characters>'."
        )
    return match.group(1)


def _safe_relative_path(value: str, field: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise VerifyError(
            f"{field} must be a safe relative path: {value!r}"
        )
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerifyError(f"Capsule not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise VerifyError(
            f"Capsule is not valid UTF-8: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise VerifyError(
            f"Invalid JSON in {path}:{exc.lineno}:{exc.colno}: "
            f"{exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise VerifyError(
            f"Capsule top-level value must be an object: {path}"
        )
    return data


def _reject_symlink_components(path: Path, root: Path) -> None:
    """Reject symlinks in an object path below the vault root."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise VerifyError(
            f"Object path escapes the vault root: {path}"
        ) from exc

    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise VerifyError(
                    f"Vault object path contains a symbolic link: {current}"
                )
        except OSError as exc:
            raise VerifyError(
                f"Cannot inspect vault path component {current}: {exc}"
            ) from exc


def resolve_capsule_object(
    *,
    capsule_path: Path,
    object_id: str,
    vault_root: Path,
) -> ObjectSpec:
    """Resolve one capsule object to its immutable vault path."""

    capsule = _load_json_object(capsule_path.absolute())
    vault_root = vault_root.expanduser().resolve()

    objects = capsule.get("objects")
    if not isinstance(objects, list):
        raise VerifyError("capsule.objects must be an array")

    matching = [
        item
        for item in objects
        if isinstance(item, dict) and item.get("id") == object_id
    ]
    if not matching:
        raise VerifyError(f"Unknown object ID: {object_id!r}")
    if len(matching) > 1:
        raise VerifyError(f"Duplicate object ID: {object_id!r}")

    item = matching[0]
    archive_path = item.get("archive_path")
    digest = item.get("digest")
    size = item.get("size")

    if not isinstance(archive_path, str):
        raise VerifyError(
            f"Object {object_id!r} has no valid archive_path"
        )
    if not isinstance(digest, str):
        raise VerifyError(
            f"Object {object_id!r} has no valid digest"
        )
    if size is not None and (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise VerifyError(
            f"Object {object_id!r} has no valid size"
        )

    relative_path = _safe_relative_path(
        archive_path,
        f"objects[{object_id!r}].archive_path",
    )

    # Deliberately avoid resolve() on the object path: resolving would follow a
    # symlink before the verifier had a chance to reject it.
    object_path = (vault_root / relative_path).absolute()
    if not _is_within(object_path, vault_root):
        raise VerifyError(
            f"Object {object_id!r} escapes the vault root"
        )

    _parse_digest(digest)

    return ObjectSpec(
        object_id=object_id,
        path=object_path,
        expected_digest=digest,
        expected_size=size,
        vault_root=vault_root,
    )


def direct_object_spec(
    *,
    path: Path,
    digest: str,
    expected_size: int | None = None,
) -> ObjectSpec:
    """Create a direct verification specification."""

    _parse_digest(digest)
    if expected_size is not None and expected_size < 0:
        raise VerifyError("Expected size cannot be negative.")

    # absolute() keeps the final symlink visible; resolve() would follow it.
    return ObjectSpec(
        object_id=None,
        path=path.expanduser().absolute(),
        expected_digest=digest,
        expected_size=expected_size,
    )


def verify_object(
    spec: ObjectSpec,
    *,
    chunk_size: int = 8 * 1024 * 1024,
) -> VerificationResult:
    """Verify one regular file without modifying it."""

    if chunk_size <= 0:
        raise VerifyError("chunk_size must be greater than zero")

    path = spec.path

    if spec.vault_root is not None:
        _reject_symlink_components(path, spec.vault_root)

    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise VerifyError(f"Object file not found: {path}") from exc
    except OSError as exc:
        raise VerifyError(f"Cannot stat object file {path}: {exc}") from exc

    if path.is_symlink():
        raise VerifyError(
            f"Object path must not be a symbolic link: {path}"
        )
    if not path.is_file():
        raise VerifyError(
            f"Object path must be a regular file: {path}"
        )

    expected_hex = _parse_digest(spec.expected_digest)
    hasher = hashlib.sha256()
    total = 0

    try:
        with path.open("rb", buffering=0) as handle:
            before = os.fstat(handle.fileno())

            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                hasher.update(block)
                total += len(block)

            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise VerifyError(f"Cannot read object file {path}: {exc}") from exc

    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
    )
    changed = [
        name
        for name in stable_fields
        if getattr(before, name, None) != getattr(after, name, None)
    ]
    if changed:
        raise VerifyError(
            "Object changed during verification "
            f"({', '.join(changed)}): {path}"
        )

    if total != before.st_size:
        raise VerifyError(
            "Read byte count does not match file size: "
            f"read {total}, stat reported {before.st_size}"
        )

    actual_hex = hasher.hexdigest()
    digest_match = actual_hex == expected_hex

    if spec.expected_size is None:
        size_match = None
    else:
        size_match = total == spec.expected_size

    verified = digest_match and size_match is not False

    return VerificationResult(
        schema=0,
        object_id=spec.object_id,
        path=str(path),
        algorithm="sha256",
        expected_digest=spec.expected_digest,
        actual_digest=f"sha256:{actual_hex}",
        digest_match=digest_match,
        expected_size=spec.expected_size,
        actual_size=total,
        size_match=size_match,
        verified=verified,
    )
