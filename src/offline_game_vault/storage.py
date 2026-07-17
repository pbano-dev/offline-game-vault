"""Atomic ingestion into the content-addressed vault store."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import hashlib
import os
from pathlib import Path
import secrets
import shutil
from typing import Any

from .verifier import (
    ObjectSpec,
    VerifyError,
    direct_object_spec,
    resolve_capsule_object,
    verify_object,
)


class IngestError(Exception):
    """Raised when an object cannot be ingested safely."""


@dataclass(frozen=True)
class IngestResult:
    schema: int
    object_id: str | None
    source: str
    destination: str
    digest: str
    bytes: int
    status: str
    source_verified: bool
    destination_verified: bool
    copied: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_object_relative_path(digest: str) -> Path:
    """Return the canonical content-addressed path for a SHA-256 digest."""

    if not digest.startswith("sha256:"):
        raise IngestError("Only sha256 digests are supported.")
    hexadecimal = digest.removeprefix("sha256:")
    if len(hexadecimal) != 64 or any(
        character not in "0123456789abcdef"
        for character in hexadecimal
    ):
        raise IngestError(
            "Digest must use lowercase form "
            "'sha256:<64 hexadecimal characters>'."
        )

    return Path(
        "objects",
        "sha256",
        hexadecimal[:2],
        hexadecimal[2:4],
        hexadecimal,
    )


def canonical_object_path(vault_root: Path, digest: str) -> Path:
    """Return the absolute canonical object path below a vault root."""

    return (
        vault_root.expanduser().resolve()
        / canonical_object_relative_path(digest)
    )



def _reject_vault_symlink_components(
    *,
    destination: Path,
    vault_root: Path,
) -> None:
    """Reject symlinked path components below the resolved vault root."""

    vault_root = vault_root.resolve()
    destination = destination.absolute()

    try:
        relative = destination.relative_to(vault_root)
    except ValueError as exc:
        raise IngestError(
            f"Destination escapes the vault root: {destination}"
        ) from exc

    current = vault_root
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise IngestError(
                "Vault destination contains a symbolic-link "
                f"directory component: {current}"
            )
        if current.exists() and not current.is_dir():
            raise IngestError(
                "Vault destination parent is not a directory: "
                f"{current}"
            )

def _copy_and_verify_source(
    *,
    source: Path,
    temporary: Path,
    expected_digest: str,
    expected_size: int | None,
    chunk_size: int,
) -> int:
    """Copy source to a new temporary file while verifying source bytes."""

    if chunk_size <= 0:
        raise IngestError("chunk_size must be greater than zero")

    source_spec = direct_object_spec(
        path=source,
        digest=expected_digest,
        expected_size=expected_size,
    )
    source_path = source_spec.path

    try:
        source_path.lstat()
    except FileNotFoundError as exc:
        raise IngestError(f"Source file not found: {source_path}") from exc
    except OSError as exc:
        raise IngestError(f"Cannot stat source file {source_path}: {exc}") from exc

    if source_path.is_symlink():
        raise IngestError(
            f"Source must not be a symbolic link: {source_path}"
        )
    if not source_path.is_file():
        raise IngestError(
            f"Source must be a regular file: {source_path}"
        )

    expected_hex = expected_digest.removeprefix("sha256:")
    hasher = hashlib.sha256()
    total = 0

    try:
        with source_path.open("rb", buffering=0) as source_handle:
            before = os.fstat(source_handle.fileno())

            with temporary.open("xb", buffering=0) as destination_handle:
                while True:
                    block = source_handle.read(chunk_size)
                    if not block:
                        break
                    destination_handle.write(block)
                    hasher.update(block)
                    total += len(block)

                destination_handle.flush()
                os.fsync(destination_handle.fileno())

            after = os.fstat(source_handle.fileno())
    except FileExistsError as exc:
        raise IngestError(
            f"Temporary ingest file already exists: {temporary}"
        ) from exc
    except OSError as exc:
        raise IngestError(
            f"Cannot copy source object {source_path}: {exc}"
        ) from exc

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
        raise IngestError(
            "Source changed during ingestion "
            f"({', '.join(changed)}): {source_path}"
        )

    if total != before.st_size:
        raise IngestError(
            "Read byte count does not match source size: "
            f"read {total}, stat reported {before.st_size}"
        )

    actual_hex = hasher.hexdigest()
    if actual_hex != expected_hex:
        raise IngestError(
            "Source digest mismatch: "
            f"expected sha256:{expected_hex}, "
            f"actual sha256:{actual_hex}"
        )

    if expected_size is not None and total != expected_size:
        raise IngestError(
            "Source size mismatch: "
            f"expected {expected_size}, actual {total}"
        )

    return total


def _promote_without_overwrite(
    *,
    temporary: Path,
    destination: Path,
) -> None:
    """Atomically publish a verified temporary file without overwriting."""

    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise IngestError(
            f"Destination appeared during ingestion: {destination}"
        ) from exc
    except OSError as exc:
        if exc.errno in {
            errno.EPERM,
            errno.EACCES,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
            errno.EXDEV,
        }:
            raise IngestError(
                "The vault filesystem cannot atomically promote the object "
                "without overwrite using a hard link. "
                f"Destination: {destination}"
            ) from exc
        raise IngestError(
            f"Cannot promote object into vault: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_existing_destination(
    *,
    destination: Path,
    expected_digest: str,
    expected_size: int | None,
    object_id: str | None,
) -> IngestResult:
    """Verify and report an object that already exists."""

    try:
        result = verify_object(
            ObjectSpec(
                object_id=object_id,
                path=destination,
                expected_digest=expected_digest,
                expected_size=expected_size,
                vault_root=None,
            )
        )
    except VerifyError as exc:
        raise IngestError(
            f"Existing destination cannot be verified: {exc}"
        ) from exc

    if not result.verified:
        raise IngestError(
            "Existing destination does not match the requested object; "
            f"refusing to overwrite: {destination}"
        )

    return IngestResult(
        schema=0,
        object_id=object_id,
        source=str(destination),
        destination=str(destination),
        digest=expected_digest,
        bytes=result.actual_size,
        status="already_present",
        source_verified=True,
        destination_verified=True,
        copied=False,
    )


def ingest_object(
    *,
    source: Path,
    destination_spec: ObjectSpec,
    chunk_size: int = 8 * 1024 * 1024,
) -> IngestResult:
    """Copy one verified regular file into its canonical vault location."""

    destination = destination_spec.path
    expected_digest = destination_spec.expected_digest
    expected_size = destination_spec.expected_size
    object_id = destination_spec.object_id

    if destination_spec.vault_root is not None:
        _reject_vault_symlink_components(
            destination=destination,
            vault_root=destination_spec.vault_root,
        )

    if destination.exists() or destination.is_symlink():
        return _verify_existing_destination(
            destination=destination,
            expected_digest=expected_digest,
            expected_size=expected_size,
            object_id=object_id,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination_spec.vault_root is not None:
        _reject_vault_symlink_components(
            destination=destination,
            vault_root=destination_spec.vault_root,
        )

    suffix = secrets.token_hex(8)
    temporary = destination.parent / (
        f".incoming-{destination.name}-{os.getpid()}-{suffix}"
    )

    try:
        total = _copy_and_verify_source(
            source=source,
            temporary=temporary,
            expected_digest=expected_digest,
            expected_size=expected_size,
            chunk_size=chunk_size,
        )

        _promote_without_overwrite(
            temporary=temporary,
            destination=destination,
        )

        try:
            stored = verify_object(
                ObjectSpec(
                    object_id=object_id,
                    path=destination,
                    expected_digest=expected_digest,
                    expected_size=expected_size,
                    vault_root=destination_spec.vault_root,
                )
            )
        except VerifyError as exc:
            raise IngestError(
                f"Stored destination verification failed: {exc}"
            ) from exc

        if not stored.verified:
            raise IngestError(
                "Stored destination does not match after promotion: "
                f"{destination}"
            )

        return IngestResult(
            schema=0,
            object_id=object_id,
            source=str(source.expanduser().absolute()),
            destination=str(destination),
            digest=expected_digest,
            bytes=total,
            status="ingested",
            source_verified=True,
            destination_verified=True,
            copied=True,
        )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def direct_destination_spec(
    *,
    vault_root: Path,
    digest: str,
    expected_size: int | None = None,
) -> ObjectSpec:
    """Create a canonical destination specification without a capsule."""

    path = canonical_object_path(vault_root, digest)
    return ObjectSpec(
        object_id=None,
        path=path,
        expected_digest=digest,
        expected_size=expected_size,
        vault_root=vault_root.expanduser().resolve(),
    )


def capsule_destination_spec(
    *,
    capsule_path: Path,
    object_id: str,
    vault_root: Path,
) -> ObjectSpec:
    """Resolve a capsule object and require its canonical store path."""

    spec = resolve_capsule_object(
        capsule_path=capsule_path,
        object_id=object_id,
        vault_root=vault_root,
    )

    canonical = canonical_object_path(vault_root, spec.expected_digest)
    if spec.path != canonical:
        raise IngestError(
            "Capsule archive_path is not canonical for its digest: "
            f"declared {spec.path}, expected {canonical}"
        )

    return spec
