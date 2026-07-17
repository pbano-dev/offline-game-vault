"""Deterministic, verified inventory of a content-addressed vault."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
from typing import Any

from .verifier import ObjectSpec, VerifyError, verify_object


class InventoryError(Exception):
    """Raised when a vault cannot be inventoried safely."""


@dataclass(frozen=True)
class InventoryObject:
    digest: str
    path: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VaultInventory:
    schema: int
    algorithm: str
    object_count: int
    total_bytes: int
    objects: tuple[InventoryObject, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objects"] = [asdict(item) for item in self.objects]
        return data

    def to_json(self) -> str:
        return (
            json.dumps(
                self.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


def _canonical_digest_from_relative_path(
    relative: Path,
) -> str:
    parts = relative.parts

    if len(parts) != 5:
        raise InventoryError(
            "Unexpected object-store path depth: "
            f"{relative.as_posix()}"
        )
    if parts[0:2] != ("objects", "sha256"):
        raise InventoryError(
            f"Unexpected object-store path: {relative.as_posix()}"
        )

    first, second, hexadecimal = parts[2], parts[3], parts[4]

    if (
        len(first) != 2
        or len(second) != 2
        or len(hexadecimal) != 64
        or any(
            character not in "0123456789abcdef"
            for character in first + second + hexadecimal
        )
    ):
        raise InventoryError(
            f"Malformed content-addressed path: {relative.as_posix()}"
        )

    if first != hexadecimal[:2] or second != hexadecimal[2:4]:
        raise InventoryError(
            f"Digest fan-out does not match filename: "
            f"{relative.as_posix()}"
        )

    return f"sha256:{hexadecimal}"


def _walk_object_store(
    vault_root: Path,
) -> list[Path]:
    object_root = vault_root / "objects"

    if not object_root.exists():
        return []
    if object_root.is_symlink():
        raise InventoryError(
            f"Object-store root must not be a symlink: {object_root}"
        )
    if not object_root.is_dir():
        raise InventoryError(
            f"Object-store root must be a directory: {object_root}"
        )

    files: list[Path] = []

    for current, directories, names in os.walk(
        object_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)

        for directory in list(directories):
            path = current_path / directory
            if path.is_symlink():
                raise InventoryError(
                    f"Object-store directory must not be a symlink: {path}"
                )

        for name in names:
            path = current_path / name
            if path.is_symlink():
                raise InventoryError(
                    f"Object-store object must not be a symlink: {path}"
                )
            if not path.is_file():
                raise InventoryError(
                    f"Object-store entry must be a regular file: {path}"
                )
            files.append(path)

    return sorted(files)


def build_inventory(
    *,
    vault_root: Path,
) -> VaultInventory:
    """Hash every canonical object and return a deterministic inventory."""

    vault_root = vault_root.expanduser().resolve()

    if not vault_root.exists():
        raise InventoryError(f"Vault root does not exist: {vault_root}")
    if not vault_root.is_dir():
        raise InventoryError(
            f"Vault root must be a directory: {vault_root}"
        )

    objects: list[InventoryObject] = []

    for path in _walk_object_store(vault_root):
        relative = path.relative_to(vault_root)
        digest = _canonical_digest_from_relative_path(relative)

        try:
            result = verify_object(
                ObjectSpec(
                    object_id=None,
                    path=path,
                    expected_digest=digest,
                    expected_size=None,
                    vault_root=vault_root,
                )
            )
        except VerifyError as exc:
            raise InventoryError(
                f"{relative.as_posix()}: {exc}"
            ) from exc

        if not result.verified:
            raise InventoryError(
                "Object content does not match its canonical path: "
                f"{relative.as_posix()} "
                f"(actual {result.actual_digest})"
            )

        objects.append(
            InventoryObject(
                digest=digest,
                path=relative.as_posix(),
                bytes=result.actual_size,
            )
        )

    objects.sort(key=lambda item: item.digest)

    return VaultInventory(
        schema=0,
        algorithm="sha256",
        object_count=len(objects),
        total_bytes=sum(item.bytes for item in objects),
        objects=tuple(objects),
    )


def write_inventory_atomic(
    *,
    inventory: VaultInventory,
    output: Path,
    vault_root: Path,
) -> None:
    """Atomically write a deterministic inventory outside objects/."""

    vault_root = vault_root.expanduser().resolve()
    output = output.expanduser().absolute()
    output_parent = output.parent.resolve()
    output = output_parent / output.name
    object_root = vault_root / "objects"

    try:
        output.relative_to(object_root)
    except ValueError:
        pass
    else:
        raise InventoryError(
            "Inventory output must not be inside the object store."
        )

    if output.exists() and output.is_symlink():
        raise InventoryError(
            f"Inventory output must not be a symlink: {output}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / (
        f".ogv-inventory-{os.getpid()}-{secrets.token_hex(8)}"
    )

    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(inventory.to_json())
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, output)

        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise InventoryError(
            f"Cannot write inventory {output}: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
