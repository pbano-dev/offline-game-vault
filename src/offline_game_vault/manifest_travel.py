"""Travel per-object manifests from the Vault to a materialization.

A per-object manifest, once generated, lives in the Vault. To make a
materialization self-verifiable, its manifests must travel with it: an
exported USB, a materialization detached from its source Vault, or a
verification years after the fact must all resolve integrity evidence
locally, without consulting the Vault.

This module orchestrates that travel:

- ``validate_manifests_present_for`` fails fast when any object lacks a
  usable manifest, before any extraction begins.
- ``copy_manifests_to_materialization`` byte-copies each manifest and its
  sidecar into ``metadata/manifests/sha256/<aa>/<bb>/<hex>`` at the
  destination.
- ``write_generated_files_manifest`` walks the destination for files that
  were produced by the composition itself (not extracted from an object),
  hashes each, and records them in ``metadata/generated-files.json``.
- ``write_receipt_sidecar`` produces a ``<receipt>.sha256`` next to the
  primary receipt so its own corruption can be detected.

The classification of destination files into "from an object" versus
"generated at composition time" is intentionally lenient: extra files a
user may have added by hand are treated as generated and hashed, not
rejected. The layer stays open to the presence of unknown material.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
import shutil
import stat
from pathlib import Path

from . import __version__
from .manifest_catalog import manifest_is_current
from .object_manifest import (
    ObjectManifestError,
    manifest_path,
    manifest_sidecar_path,
)


GENERATED_FILES_MANIFEST = "metadata/generated-files.json"
"""Destination-relative path where the generated-files manifest lives."""

MANIFESTS_SUBTREE = "metadata/manifests"
"""Destination-relative root under which per-object manifests are copied."""


class ManifestTravelError(Exception):
    """Raised when manifest travel fails while composing a materialization."""


# ------------------------------------------------------ pre-flight


def validate_manifests_present_for(
    *,
    vault_root: Path,
    digests: Iterable[str],
) -> None:
    """Fail fast when any preserved object lacks a valid manifest.

    Called at the start of ``compose_*`` before any extraction. If the
    materialization cannot end up self-verifiable, the caller learns before
    tens of gigabytes have been written.
    """
    vault_root = vault_root.expanduser().resolve()
    missing: list[str] = []
    for digest in digests:
        try:
            target = manifest_path(vault_root, digest)
        except ObjectManifestError as exc:
            missing.append(f"{digest} ({exc})")
            continue
        if not manifest_is_current(target, digest):
            missing.append(digest)
    if missing:
        listed = ", ".join(sorted(set(missing)))
        raise ManifestTravelError(
            "Cannot compose: the following objects lack a valid per-object "
            "manifest in the Vault. Run "
            "`ogv generate-missing-manifests --collection-root <root>` "
            f"first. Missing: {listed}"
        )


# --------------------------------------------------- manifest copy


def copy_manifests_to_materialization(
    *,
    vault_root: Path,
    destination: Path,
    digests: Iterable[str],
) -> list[Path]:
    """Copy per-object manifests + sidecars into ``destination/metadata/manifests/``.

    Byte-identical copies; the manifest's own sidecar keeps its integrity
    check after transport. Returns the list of paths written under
    ``destination``, for testing and diagnostics.
    """
    vault_root = vault_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    written: list[Path] = []
    for digest in digests:
        source = manifest_path(vault_root, digest)
        source_sidecar = manifest_sidecar_path(source)
        if not source.is_file() or not source_sidecar.is_file():
            raise ManifestTravelError(
                f"Manifest for {digest} vanished between validation and copy."
            )
        target = _local_manifest_path(destination, digest)
        target_sidecar = manifest_sidecar_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        shutil.copy2(source_sidecar, target_sidecar)
        written.extend((target, target_sidecar))
    return written


# --------------------------------------------- generated-files manifest


def write_generated_files_manifest(
    *,
    destination: Path,
    object_owned_prefixes: set[str],
    excluded_paths: set[Path] | None = None,
) -> Path:
    """Hash every "generated" file at the destination into a single manifest.

    A file is "generated" when it is a regular file whose top-level
    destination-relative component is NOT one of ``object_owned_prefixes``
    (subtrees planted by object extraction) and whose absolute path is NOT
    in ``excluded_paths`` (self-references such as receipt sidecars).

    Extra files a user may have added by hand fall into the generated
    bucket and are hashed; they are recorded, not rejected. This keeps the
    layer "based on existence", not enumeration.
    """
    destination = destination.expanduser().resolve()
    manifest_file = destination / GENERATED_FILES_MANIFEST
    excluded = {p.resolve() for p in (excluded_paths or set())}
    excluded.add(manifest_file.resolve())
    manifests_subtree = (destination / MANIFESTS_SUBTREE).resolve()

    entries: list[dict[str, object]] = []
    for absolute in sorted(_walk_regular_files(destination)):
        relative = absolute.relative_to(destination)
        top = relative.parts[0] if relative.parts else ""
        if top in object_owned_prefixes:
            continue
        if absolute in excluded:
            continue
        # ``metadata/manifests/**`` is excluded by prefix: it has its own
        # sidecar integrity, so re-hashing it here is noise.
        try:
            absolute.relative_to(manifests_subtree)
            continue
        except ValueError:
            pass
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256_file(absolute),
                "bytes": absolute.stat().st_size,
            }
        )

    document = {
        "schema": 0,
        "created_at": _now(),
        "generator": f"offline-game-vault/{__version__}",
        "files": entries,
    }
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    )
    manifest_file.write_text(payload, encoding="utf-8")
    return manifest_file


# ------------------------------------------------- receipt sidecar


def write_receipt_sidecar(receipt_path: Path) -> Path:
    """Write ``<receipt>.sha256`` next to a receipt file.

    Format is ``sha256sum``-compatible: ``<hex>  <basename>\\n``. Detects
    accidental corruption of the receipt itself; deliberate manipulation is
    outside the model.
    """
    receipt_path = receipt_path.expanduser().resolve()
    if not receipt_path.is_file():
        raise ManifestTravelError(f"Receipt not found: {receipt_path}")
    digest_hex = _sha256_file(receipt_path)
    sidecar = receipt_path.with_name(receipt_path.name + ".sha256")
    sidecar.write_text(
        f"{digest_hex}  {receipt_path.name}\n", encoding="utf-8"
    )
    return sidecar


# ------------------------------------------------------- helpers


def _local_manifest_path(destination: Path, digest: str) -> Path:
    """Map an object digest to its manifest path under ``destination``."""
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ManifestTravelError(
            f"Digest must be 'sha256:<hex>': {digest!r}"
        )
    hex_digest = digest.removeprefix("sha256:")
    if len(hex_digest) != 64:
        raise ManifestTravelError(f"Malformed digest: {digest!r}")
    return (
        destination
        / MANIFESTS_SUBTREE
        / "sha256"
        / hex_digest[:2]
        / hex_digest[2:4]
        / hex_digest
    )


def _walk_regular_files(root: Path):
    """Yield every regular file (not symlink) under ``root``, sorted."""
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            yield from _walk_regular_files(entry)
        elif stat.S_ISREG(info.st_mode):
            yield entry


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
