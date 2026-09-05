"""Deterministic human-readable references over the immutable object store."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
from typing import Any


class CatalogViewError(RuntimeError):
    pass


_PORTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CatalogViewError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogViewError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogViewError(f"{label} must contain a JSON object")
    return value


def _portable(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PORTABLE.fullmatch(value):
        raise CatalogViewError(f"{label} is not a portable identifier")
    return value


def _kind(roles: set[str]) -> str:
    for role, kind in (
        ("game_payload", "game"),
        ("runner", "runner"),
        ("runtime", "runtime"),
        ("supplemental_content", "supplemental-content"),
        ("prefix_baseline", "prefix-baseline"),
        ("save", "persistent-state"),
    ):
        if role in roles:
            return kind
    return "object"


def _payload(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _catalog_documents(collection_root: Path) -> dict[PurePosixPath, bytes]:
    inventory = _json(
        collection_root / "01_IMMUTABLE_VAULT/VAULT_INVENTORY.json",
        "VAULT_INVENTORY.json",
    )
    raw_objects = inventory.get("objects")
    if not isinstance(raw_objects, list):
        raise CatalogViewError("VAULT_INVENTORY.json has no objects array")

    records: dict[str, dict[str, Any]] = {}
    for raw in raw_objects:
        if not isinstance(raw, dict):
            raise CatalogViewError("Inventory contains a non-object entry")
        digest = raw.get("digest")
        path = raw.get("path")
        size = raw.get("bytes")
        if (
            not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or not isinstance(path, str)
            or not isinstance(size, int)
            or size < 0
        ):
            raise CatalogViewError("Inventory object fields are invalid")
        hexadecimal = digest.removeprefix("sha256:")
        expected = f"objects/sha256/{hexadecimal[:2]}/{hexadecimal[2:4]}/{hexadecimal}"
        if path != expected:
            raise CatalogViewError(
                f"Inventory path is not canonical for {digest}: {path}"
            )
        if digest in records:
            raise CatalogViewError(f"Duplicate inventory digest: {digest}")
        records[digest] = {
            "path": path,
            "bytes": size,
            "roles": set(),
            "ids": set(),
            "shared": False,
            "consumers": [],
        }

    capsules_root = collection_root / "02_CAPSULES"
    if capsules_root.is_symlink() or not capsules_root.is_dir():
        raise CatalogViewError("Collection has no regular 02_CAPSULES directory")

    games: list[tuple[str, str, str, str]] = []
    capsule_count = 0
    for capsule_path in sorted(capsules_root.glob("*/capsule.json")):
        capsule = _json(capsule_path, "capsule.json")
        capsule_id = _portable(capsule.get("capsule_id"), "capsule_id")
        if capsule_path.parent.name != capsule_id:
            raise CatalogViewError(
                f"Capsule directory disagrees with capsule_id: {capsule_path}"
            )
        game = capsule.get("game")
        title = game.get("title") if isinstance(game, dict) else None
        if not isinstance(title, str) or not title.strip():
            raise CatalogViewError(f"Capsule {capsule_id} has no game title")
        objects = capsule.get("objects")
        if not isinstance(objects, list):
            raise CatalogViewError(f"Capsule {capsule_id} has no objects array")
        capsule_count += 1
        for declaration in objects:
            if not isinstance(declaration, dict):
                raise CatalogViewError(
                    f"Capsule {capsule_id} contains a non-object declaration"
                )
            object_id = _portable(
                declaration.get("id"), f"{capsule_id} object id"
            )
            digest = declaration.get("digest")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise CatalogViewError(
                    f"Capsule {capsule_id} object {object_id} has invalid digest"
                )
            record = records.get(digest)
            if record is None:
                raise CatalogViewError(
                    f"Capsule {capsule_id} references absent object {digest}"
                )
            declared_path = declaration.get("archive_path")
            if declared_path != record["path"]:
                raise CatalogViewError(
                    f"Capsule {capsule_id} path disagrees with inventory for {digest}"
                )
            declared_size = declaration.get("size")
            if declared_size is not None and declared_size != record["bytes"]:
                raise CatalogViewError(
                    f"Capsule {capsule_id} size disagrees with inventory for {digest}"
                )
            raw_roles = declaration.get("roles", [])
            if not isinstance(raw_roles, list) or not all(
                isinstance(role, str) and role for role in raw_roles
            ):
                raise CatalogViewError(
                    f"Capsule {capsule_id} object {object_id} has invalid roles"
                )
            record["roles"].update(raw_roles)
            record["ids"].add(object_id)
            record["shared"] = bool(record["shared"] or declaration.get("shared"))
            record["consumers"].append(
                {
                    "capsule_id": capsule_id,
                    "object_id": object_id,
                    "title": title,
                }
            )
            games.append((capsule_id, object_id, digest, title))

    references: dict[str, dict[str, Any]] = {}
    for digest, record in sorted(records.items()):
        identifiers = sorted(record["ids"])
        roles = sorted(record["roles"])
        consumers = sorted(
            record["consumers"],
            key=lambda item: (item["capsule_id"], item["object_id"]),
        )
        references[digest] = {
            "schema": 0,
            "contract": "ogv-object-reference-v1",
            "digest": digest,
            "object_id": identifiers[0] if identifiers else None,
            "aliases": identifiers[1:],
            "roles": roles,
            "kind": _kind(set(roles)),
            "scope": (
                "unreferenced"
                if not consumers
                else "shared"
                if record["shared"] or len({x["capsule_id"] for x in consumers}) > 1
                else "game"
            ),
            "bytes": record["bytes"],
            "path": f"01_IMMUTABLE_VAULT/{record['path']}",
            "consumers": consumers,
        }

    documents: dict[PurePosixPath, bytes] = {}
    for digest, reference in references.items():
        hexadecimal = digest.removeprefix("sha256:")
        documents[
            PurePosixPath("BY_DIGEST", hexadecimal[:2], hexadecimal + ".ogvref")
        ] = _payload(reference)
        if reference["scope"] == "unreferenced":
            documents[
                PurePosixPath("UNREFERENCED", hexadecimal + ".ogvref")
            ] = _payload(reference)
        elif reference["scope"] == "shared":
            name = (reference["object_id"] or "object") + "--" + hexadecimal[:12]
            documents[PurePosixPath("SHARED", name + ".ogvref")] = _payload(
                reference
            )

    for capsule_id, object_id, digest, _title in sorted(games):
        documents[
            PurePosixPath("GAMES", capsule_id, object_id + ".ogvref")
        ] = _payload(references[digest])

    referenced = sum(bool(item["consumers"]) for item in references.values())
    summary = {
        "schema": 0,
        "contract": "ogv-human-catalog-v1",
        "capsule_count": capsule_count,
        "object_count": len(references),
        "referenced_object_count": referenced,
        "unreferenced_object_count": len(references) - referenced,
        "game_reference_count": len(games),
    }
    documents[PurePosixPath("CATALOG.json")] = _payload(summary)
    documents[PurePosixPath("README.md")] = (
        "# OfflineGameVault catalog\n\n"
        "This directory is a deterministic, regenerable view over canonical "
        "capsules and immutable objects. `.ogvref` files are JSON references; "
        "they are not game data and are never deletion authority.\n"
    ).encode("utf-8")
    return documents


def _write_tree(root: Path, documents: dict[PurePosixPath, bytes]) -> None:
    for directory in ("GAMES", "SHARED", "BY_DIGEST", "UNREFERENCED"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for relative, payload in documents.items():
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        os.chmod(target, 0o644)


def build_catalog(
    collection_root: Path,
    *,
    output: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = collection_root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise CatalogViewError("Collection root must be a regular directory")
    root = root.resolve()
    documents = _catalog_documents(root)
    target = (output or (root / "00_CATALOG")).expanduser().absolute()
    summary = json.loads(documents[PurePosixPath("CATALOG.json")])
    if dry_run:
        return {**summary, "status": "catalog-build-valid", "written": False}
    if target.is_symlink():
        raise CatalogViewError("Catalog target must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    staging = target.parent / f".{target.name}.staging-{token}"
    backup = target.parent / f".{target.name}.backup-{token}"
    try:
        staging.mkdir(mode=0o700)
        _write_tree(staging, documents)
        if target.exists():
            if not target.is_dir():
                raise CatalogViewError("Catalog target is not a directory")
            os.replace(target, backup)
        os.replace(staging, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    return {**summary, "status": "catalog-built", "written": True}


def verify_catalog(collection_root: Path) -> dict[str, Any]:
    root = collection_root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise CatalogViewError("Collection root must be a regular directory")
    root = root.resolve()
    expected = _catalog_documents(root)
    catalog = root / "00_CATALOG"
    if catalog.is_symlink() or not catalog.is_dir():
        raise CatalogViewError("Collection has no regular 00_CATALOG")
    for name in ("GAMES", "SHARED", "BY_DIGEST", "UNREFERENCED"):
        directory = catalog / name
        if directory.is_symlink() or not directory.is_dir():
            raise CatalogViewError(
                f"Catalog is missing regular directory: {name}"
            )
    actual: dict[PurePosixPath, bytes] = {}
    for path in sorted(catalog.rglob("*")):
        if path.is_symlink():
            raise CatalogViewError(f"Catalog contains a symlink: {path}")
        if path.is_file():
            actual[PurePosixPath(path.relative_to(catalog).as_posix())] = (
                path.read_bytes()
            )
        elif not path.is_dir():
            raise CatalogViewError(f"Catalog contains a special file: {path}")
    missing = sorted(str(path) for path in expected.keys() - actual.keys())
    extra = sorted(str(path) for path in actual.keys() - expected.keys())
    changed = sorted(
        str(path)
        for path in expected.keys() & actual.keys()
        if expected[path] != actual[path]
    )
    if missing or extra or changed:
        raise CatalogViewError(
            "Catalog is stale: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    summary = json.loads(expected[PurePosixPath("CATALOG.json")])
    return {**summary, "status": "catalog-verified", "verified": True}
