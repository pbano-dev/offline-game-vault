"""Standalone manifest-based verification helper for materializations.

Copied into every materialization alongside the backend runtime module so
that ``VERIFICAR.sh`` can rehash the tree offline. Depends only on the
Python standard library so that a materialization on a USB stick years
from now needs no installed package to verify itself.

The check follows the "based on existence, not enumeration" principle:

- **Hard failures** — receipt sidecar corruption, missing or mismatched
  entries in ``metadata/generated-files.json``, and corruption of any
  per-object manifest sidecar. These are the artefacts the composition
  explicitly put in place with known-good hashes; corruption of any of
  them means the materialization's own evidence is untrustworthy.

- **Informational counts** — how many destination files matched a
  per-object manifest entry, and how many manifest entries were declared
  in the Vault but not found at the destination. Neither of these is a
  failure: materializations legitimately discard object content (UMU
  places only a subset of runner and runtime archives), and users may
  add files by hand.

For deep per-file verification of object content the manifests carry
their own sha256sum-compatible bodies; a user can run ``sha256sum -c``
against them by hand if the destination layout is known.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import hashlib
import json
import stat
from pathlib import Path
from typing import Any


GENERATED_FILES_MANIFEST = "metadata/generated-files.json"
METADATA_SUBTREE = "metadata"


class ManifestCheckError(Exception):
    """Raised when verification fails; message summarises the failures."""


def verify_by_manifests(
    *,
    root: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    """Verify a materialization against the manifests that travelled with it.

    ``root`` is the materialization root (absolute). ``receipt_path`` is
    the backend's primary receipt at ``root``.

    Raises ``ManifestCheckError`` when a hard check fails, with a message
    listing every failure. The returned dict is the same structured
    report even when it succeeds, for JSON consumers.
    """
    root = root.resolve()
    report: dict[str, Any] = {
        "receipt_verified": False,
        "generated_files_verified": 0,
        "generated_files_failed": [],
        "manifests_verified": 0,
        "manifests_corrupted": [],
        "object_files_matched": 0,
        "object_files_declared": 0,
        "object_files_not_found_at_destination": 0,
    }
    failures: list[str] = []

    # 1. Receipt sidecar. Fail-fast: if the receipt was corrupted, no
    # downstream evidence is trustworthy.
    try:
        _verify_receipt_sidecar(receipt_path)
        report["receipt_verified"] = True
    except ManifestCheckError as exc:
        raise ManifestCheckError(str(exc))

    # 2. Generated files declared in metadata/generated-files.json.
    generated_paths = _verify_generated_files(root, report, failures)

    # 3. Per-object manifests: sidecar check + parse.
    manifests = _load_manifests(root, report, failures)

    # 4. Content matching between manifests and destination (informational).
    _match_object_content(
        root=root,
        manifests=manifests,
        generated_paths=generated_paths,
        report=report,
    )

    if failures:
        raise ManifestCheckError(
            "Manifest-based verification failed:\n  - "
            + "\n  - ".join(failures)
        )
    return report


# --------------------------------------------------------- receipt


def _verify_receipt_sidecar(receipt_path: Path) -> None:
    receipt_path = receipt_path.resolve()
    if not receipt_path.is_file():
        raise ManifestCheckError(f"Receipt absent: {receipt_path.name}")
    sidecar = receipt_path.with_name(receipt_path.name + ".sha256")
    if not sidecar.is_file():
        raise ManifestCheckError(
            f"Receipt sidecar absent: {sidecar.name}"
        )
    recorded_line = sidecar.read_text(encoding="utf-8").strip()
    recorded_hex = recorded_line.split(None, 1)[0] if recorded_line else ""
    if not _is_sha256_hex(recorded_hex):
        raise ManifestCheckError(
            f"Receipt sidecar is malformed: {sidecar.name}"
        )
    actual_hex = _sha256_file(receipt_path)
    if actual_hex != recorded_hex:
        raise ManifestCheckError(
            f"Receipt sidecar mismatch: expected {recorded_hex}, "
            f"got {actual_hex}"
        )


# ----------------------------------------------- generated files


def _verify_generated_files(
    root: Path,
    report: dict[str, Any],
    failures: list[str],
) -> set[Path]:
    """Verify entries in generated-files.json, returning absolute paths seen.

    The returned set is used by the object-content matching step to skip
    generated files (which have already been verified here).
    """
    manifest_file = root / GENERATED_FILES_MANIFEST
    seen: set[Path] = set()
    if not manifest_file.is_file():
        failures.append(
            f"Generated-files manifest absent: {GENERATED_FILES_MANIFEST}"
        )
        return seen
    try:
        document = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(
            f"Generated-files manifest could not be parsed: {exc}"
        )
        return seen
    entries = document.get("files")
    if not isinstance(entries, list):
        failures.append(
            "Generated-files manifest has no 'files' array."
        )
        return seen
    verified = 0
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("Generated-files entry is invalid.")
            continue
        relative = entry.get("path")
        expected = entry.get("sha256")
        expected_size = entry.get("bytes")
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or not _is_sha256_hex(expected)
        ):
            failures.append("Generated-files entry has invalid fields.")
            continue
        target = root / relative
        seen.add(target.resolve())
        if target.is_symlink() or not target.is_file():
            report["generated_files_failed"].append(
                {"path": relative, "reason": "absent"}
            )
            failures.append(f"Generated file absent: {relative}")
            continue
        actual = _sha256_file(target)
        if actual != expected:
            report["generated_files_failed"].append(
                {
                    "path": relative,
                    "reason": "digest mismatch",
                    "expected": expected,
                    "actual": actual,
                }
            )
            failures.append(f"Generated file digest mismatch: {relative}")
            continue
        if isinstance(expected_size, int) and not isinstance(
            expected_size, bool
        ):
            actual_size = target.stat().st_size
            if actual_size != expected_size:
                report["generated_files_failed"].append(
                    {
                        "path": relative,
                        "reason": "size mismatch",
                        "expected": expected_size,
                        "actual": actual_size,
                    }
                )
                failures.append(
                    f"Generated file size mismatch: {relative}"
                )
                continue
        verified += 1
    report["generated_files_verified"] = verified
    return seen


# ---------------------------------------- per-object manifests


def _load_manifests(
    root: Path,
    report: dict[str, Any],
    failures: list[str],
) -> list[dict[str, Any]]:
    manifests_root = root / METADATA_SUBTREE / "manifests"
    if not manifests_root.is_dir():
        failures.append(
            f"Manifests subtree absent: {METADATA_SUBTREE}/manifests"
        )
        return []
    loaded: list[dict[str, Any]] = []
    for absolute in sorted(_walk_regular_files(manifests_root)):
        if absolute.name.endswith(".sha256"):
            continue
        sidecar = absolute.with_name(absolute.name + ".sha256")
        if not sidecar.is_file():
            report["manifests_corrupted"].append(absolute.name)
            failures.append(f"Manifest sidecar absent: {absolute.name}")
            continue
        recorded_line = sidecar.read_text(encoding="utf-8").strip()
        recorded_hex = (
            recorded_line.split(None, 1)[0] if recorded_line else ""
        )
        if not _is_sha256_hex(recorded_hex):
            report["manifests_corrupted"].append(absolute.name)
            failures.append(f"Manifest sidecar malformed: {absolute.name}")
            continue
        actual_hex = _sha256_file(absolute)
        if actual_hex != recorded_hex:
            report["manifests_corrupted"].append(absolute.name)
            failures.append(f"Manifest sidecar mismatch: {absolute.name}")
            continue
        parsed = _parse_manifest(absolute)
        if parsed is None:
            report["manifests_corrupted"].append(absolute.name)
            failures.append(f"Manifest is malformed: {absolute.name}")
            continue
        loaded.append(parsed)
        report["manifests_verified"] += 1
    return loaded


def _parse_manifest(manifest_path: Path) -> dict[str, Any] | None:
    """Parse the canonical object manifest format.

    Format (see ``offline_game_vault.object_manifest.format_manifest``):

        manifest_schema:0
        object_digest:sha256:<hex>
        object_size:<n>
        generated_at:<iso>
        generator:<str>
        file_count:<n>
        total_bytes:<n>
                                            (blank line separator)
        <hex_digest> <size> <path>
        <hex_digest> <size> <path>
        ...

    Returns ``None`` on any parse failure so the caller can list the
    manifest as corrupted.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if "\n\n" not in text:
        return None
    header_block, body_block = text.split("\n\n", 1)

    header: dict[str, str] = {}
    for raw in header_block.splitlines():
        if ":" not in raw:
            return None
        key, _, value = raw.partition(":")
        header[key] = value

    object_digest = header.get("object_digest", "")
    entries: list[dict[str, Any]] = []
    for raw in body_block.splitlines():
        if not raw:
            continue
        parts = raw.split(" ", 2)
        if len(parts) != 3:
            return None
        hex_digest, size_str, path = parts
        if not _is_sha256_hex(hex_digest):
            return None
        try:
            size = int(size_str)
        except ValueError:
            return None
        entries.append(
            {"sha256": hex_digest, "size": size, "path": path}
        )
    return {
        "object_digest": object_digest,
        "manifest_name": manifest_path.name,
        "entries": entries,
    }


# ------------------------------------ object content (informational)


def _match_object_content(
    *,
    root: Path,
    manifests: list[dict[str, Any]],
    generated_paths: set[Path],
    report: dict[str, Any],
) -> None:
    """Count how many destination files match a per-object manifest entry.

    This walks the whole destination once, hashing every regular file that
    is not part of the metadata/ subtree (which has its own sidecar
    integrity) and not already verified as a generated file. Matches to
    (size, hex) entries in any manifest are counted; unmatched files and
    unmatched manifest entries are counted too, but neither is a failure.
    Extra files a user added by hand are silently absorbed; object content
    a materialization did not place at the destination stays counted only
    as a "declared but not found" number.
    """
    expected: Counter[tuple[int, str]] = Counter()
    for manifest in manifests:
        for entry in manifest["entries"]:
            expected[(entry["size"], entry["sha256"])] += 1
    report["object_files_declared"] = sum(expected.values())

    metadata_root = (root / METADATA_SUBTREE).resolve()
    matched = 0
    for absolute in _walk_regular_files(root):
        try:
            absolute.relative_to(metadata_root)
            continue
        except ValueError:
            pass
        if absolute.resolve() in generated_paths:
            continue
        size = absolute.stat().st_size
        actual_hex = _sha256_file(absolute)
        key = (size, actual_hex)
        if expected[key] > 0:
            expected[key] -= 1
            matched += 1
    report["object_files_matched"] = matched
    report["object_files_not_found_at_destination"] = sum(
        v for v in expected.values() if v > 0
    )


# --------------------------------------------------------- helpers


def _walk_regular_files(root: Path):
    """Yield every regular file (skipping symlinks) under ``root``."""
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


def _is_sha256_hex(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
