"""Catalog manifests across the whole Vault.

The per-object manifest layer is a leaf: it hashes one archive and writes one
file pair. This module is the layer above it — it walks the Vault, learns
what objects exist, discovers each object's format by inspecting the capsules
that declare it, and orchestrates ``generate_object_manifest`` over the set,
skipping objects that already have a valid manifest.

Nothing here changes the meaning of a manifest. It only decides *which*
manifests need to exist and drives their creation without asking the operator
to name each object individually.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterator

from .object_manifest import (
    ObjectManifestError,
    compute_sidecar_digest,
    detect_source_root,
    format_manifest,
    generate_object_manifest,
    manifest_path,
    manifest_sidecar_path,
    read_manifest,
    write_manifest_atomically,
)


class ManifestCatalogError(Exception):
    """Raised when the catalog cannot be walked or a batch run fails."""


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    """One object as it appears in the Vault, with what we learned about it.

    ``format`` may be ``None`` if no capsule that declares this object was
    found; batch generation skips those with an explanatory reason instead of
    guessing.
    """

    digest: str
    archive: Path
    size: int
    format: str | None
    declared_by: tuple[str, ...] = ()


@dataclass(slots=True)
class BatchResult:
    """Aggregate outcome of a batch run."""

    generated: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": 0,
            "generated_count": len(self.generated),
            "already_present_count": len(self.already_present),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "generated": list(self.generated),
            "already_present": list(self.already_present),
            "skipped": [
                {"digest": digest, "reason": reason}
                for digest, reason in self.skipped
            ],
            "failed": [
                {"digest": digest, "reason": reason}
                for digest, reason in self.failed
            ],
        }

    @property
    def has_failures(self) -> bool:
        return bool(self.failed)


# ------------------------------------------------------- discovery


def read_vault_inventory(vault_root: Path) -> list[dict[str, object]]:
    """Return the ``objects`` list of ``VAULT_INVENTORY.json``.

    Raises ``ManifestCatalogError`` when the file is missing or malformed;
    this is a precondition for anything else in the module.
    """
    inventory_file = vault_root / "VAULT_INVENTORY.json"
    try:
        payload = inventory_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestCatalogError(
            f"Cannot read {inventory_file}: {exc}"
        ) from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ManifestCatalogError(
            f"{inventory_file} is not valid JSON: {exc}"
        ) from exc
    objects = document.get("objects")
    if not isinstance(objects, list):
        raise ManifestCatalogError(
            f"{inventory_file} has no 'objects' array."
        )
    return objects


def _iter_capsule_paths(collection_root: Path) -> Iterator[Path]:
    capsules_root = collection_root / "02_CAPSULES"
    if not capsules_root.is_dir():
        return
    for entry in sorted(capsules_root.iterdir()):
        candidate = entry / "capsule.json"
        if candidate.is_file():
            yield candidate


def _collect_declared_formats(
    collection_root: Path,
) -> dict[str, tuple[str, list[str]]]:
    """Return ``{digest: (format, [capsule_id, ...])}`` from every capsule.

    A digest appearing in more than one capsule must have the same declared
    format; a conflict is a data problem worth surfacing rather than hiding.
    """
    formats: dict[str, tuple[str, list[str]]] = {}
    for capsule_path in _iter_capsule_paths(collection_root):
        try:
            document = json.loads(
                capsule_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        capsule_id = str(document.get("capsule_id") or capsule_path.parent.name)
        for entry in document.get("objects", []):
            if not isinstance(entry, dict):
                continue
            digest = entry.get("digest")
            fmt = entry.get("format")
            if not isinstance(digest, str) or not isinstance(fmt, str):
                continue
            existing = formats.get(digest)
            if existing is None:
                formats[digest] = (fmt, [capsule_id])
            elif existing[0] != fmt:
                raise ManifestCatalogError(
                    f"Object {digest} is declared with format "
                    f"{existing[0]!r} by {existing[1]} and with "
                    f"format {fmt!r} by {capsule_id}."
                )
            else:
                existing[1].append(capsule_id)
    return formats


def _format_from_index_label(label: str) -> str | None:
    """Deduce the archive format from an ``INDEX.json`` label.

    Shared components (runners, backends, dependencies) are not always
    declared in a capsule; their format lives in the label of their
    ``INDEX.json`` entry, e.g. ``soda-9.0-1.tar.gz`` or
    ``bottles-flatpak-64.1-x86_64-stable.tar.zst``.
    """
    lowered = label.lower()
    for suffix, fmt in (
        (".tar.zst", "tar.zst"),
        (".tar.gz", "tar.gz"),
        (".tgz", "tar.gz"),
        (".tzst", "tar.zst"),
        (".zip", "zip"),
        (".tar", "tar"),
    ):
        if lowered.endswith(suffix):
            return fmt
    return None


def _collect_index_formats(collection_root: Path) -> dict[str, tuple[str, list[str]]]:
    """Return ``{digest: (format, [role, ...])}`` for entries in INDEX.json.

    Complements ``_collect_declared_formats`` so shared components declared
    only in ``INDEX.json`` are still processed by the batch instead of being
    skipped as "no capsule declares this object's format".
    """
    index_file = collection_root / "INDEX.json"
    if not index_file.is_file():
        return {}
    try:
        document = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    formats: dict[str, tuple[str, list[str]]] = {}
    for entry in document.get("objects", []):
        if not isinstance(entry, dict):
            continue
        digest_hex = entry.get("sha256")
        label = entry.get("label")
        role = entry.get("role")
        if not isinstance(digest_hex, str) or not isinstance(label, str):
            continue
        fmt = _format_from_index_label(label)
        if fmt is None:
            continue
        digest = f"sha256:{digest_hex}"
        marker = f"INDEX.json:{role}" if isinstance(role, str) else "INDEX.json"
        formats.setdefault(digest, (fmt, [marker]))
    return formats


def scan_vault(collection_root: Path) -> list[ObjectRecord]:
    """Enumerate every preserved object in the Vault, ready for batch work.

    ``collection_root`` is the collection root (the directory that contains
    ``01_IMMUTABLE_VAULT`` and ``02_CAPSULES``), not the immutable root
    itself. That keeps the caller consistent with the rest of the CLI.
    """
    root = collection_root.expanduser().resolve()
    immutable = root / "01_IMMUTABLE_VAULT"
    if not immutable.is_dir():
        raise ManifestCatalogError(
            f"Immutable Vault root is missing: {immutable}"
        )

    formats_index = _collect_declared_formats(root)
    index_fallback = _collect_index_formats(root)
    records: list[ObjectRecord] = []

    for entry in read_vault_inventory(immutable):
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        archive_relative = entry.get("path")
        size = entry.get("bytes")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            continue
        if not isinstance(archive_relative, str) or not archive_relative:
            continue
        archive_path = (immutable / archive_relative).resolve()
        try:
            archive_path.relative_to(immutable)
        except ValueError:
            continue
        fmt_info = formats_index.get(digest) or index_fallback.get(digest)
        fmt = fmt_info[0] if fmt_info else None
        declared_by = tuple(fmt_info[1]) if fmt_info else ()
        records.append(
            ObjectRecord(
                digest=digest,
                archive=archive_path,
                size=int(size) if isinstance(size, int) else 0,
                format=fmt,
                declared_by=declared_by,
            )
        )
    return records


# ------------------------------------------------------- batch


def manifest_is_current(
    manifest_file: Path, expected_object_digest: str
) -> bool:
    """Return ``True`` when a manifest at ``manifest_file`` is usable as-is.

    "Usable" means: the file and its sidecar exist, integrity of both
    checks out, and the manifest names the expected object. Anything else is
    treated as absent and triggers regeneration in a batch run.
    """
    sidecar = manifest_sidecar_path(manifest_file)
    if not manifest_file.is_file() or not sidecar.is_file():
        return False
    try:
        manifest = read_manifest(manifest_file)
    except ObjectManifestError:
        return False
    return manifest.object_digest == expected_object_digest


def generate_missing_manifests(
    *,
    collection_root: Path,
    vault_root: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: callable | None = None,
) -> BatchResult:
    """Ensure every object in the Vault has a valid manifest.

    ``vault_root`` defaults to ``collection_root / '01_IMMUTABLE_VAULT'``;
    it can be overridden for tests. Objects whose format cannot be resolved
    are skipped with a reason rather than guessed at, because writing a
    manifest with the wrong format would produce false evidence.

    ``limit`` caps the number of objects that would be *processed* (missing
    or invalid); objects already covered by a valid manifest do not count.
    """
    root = collection_root.expanduser().resolve()
    immutable = (
        (vault_root or (root / "01_IMMUTABLE_VAULT"))
        .expanduser()
        .resolve()
    )

    records = scan_vault(root)
    result = BatchResult()
    processed = 0

    for record in records:
        target = manifest_path(immutable, record.digest)
        if manifest_is_current(target, record.digest):
            result.already_present.append(record.digest)
            if progress is not None:
                progress("skip-current", record)
            continue

        if record.format is None:
            result.skipped.append(
                (record.digest, "no capsule declares this object's format")
            )
            if progress is not None:
                progress("skip-noformat", record)
            continue

        if not record.archive.is_file():
            result.skipped.append(
                (record.digest, f"archive not found: {record.archive}")
            )
            if progress is not None:
                progress("skip-missing", record)
            continue

        if limit is not None and processed >= limit:
            result.skipped.append(
                (record.digest, "limit reached")
            )
            continue

        processed += 1
        if progress is not None:
            progress("start", record)
        try:
            source_root = detect_source_root(record.archive, record.format)
            manifest = generate_object_manifest(
                archive=record.archive,
                archive_format=record.format,
                source_root=source_root,
                object_digest=record.digest,
                object_size=(
                    record.size
                    if record.size
                    else record.archive.stat().st_size
                ),
            )
            if dry_run:
                # Compute the sidecar path anyway to surface what would run.
                _payload = format_manifest(manifest)
                _digest = compute_sidecar_digest(_payload)
                result.generated.append(record.digest)
            else:
                write_manifest_atomically(manifest, target)
                result.generated.append(record.digest)
            if progress is not None:
                progress("done", record)
        except (ObjectManifestError, OSError) as exc:
            result.failed.append((record.digest, str(exc)))
            if progress is not None:
                progress("fail", record)

    return result
