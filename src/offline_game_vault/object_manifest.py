"""Per-object file manifests for post-materialization verification.

An object manifest lists every regular file inside a preserved object with its
SHA-256 digest, so a materialization can be verified byte-for-byte against
what the Vault recorded when the object was ingested. Manifests are the
evidence that lets an exported USB verify itself years later without ever
consulting the Vault it came from.

Layout inside the immutable Vault:

    objects/sha256/<aa>/<bb>/<hex>       the preserved archive (existing)
    manifests/sha256/<aa>/<bb>/<hex>     its manifest (this module)
    manifests/sha256/<aa>/<bb>/<hex>.sha256   the manifest's own digest

The manifest itself is deterministic: the same object always produces the
same bytes, so its digest is a stable identifier and any drift is detectable.
Manifest generation is idempotent: running it twice on the same object either
returns the existing manifest or rewrites it identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Iterable
import zipfile

from . import __version__


MANIFEST_SCHEMA = 0
"""Schema tag written in the manifest header. Bumped only on incompatible changes."""

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# Header keys, in fixed order for deterministic output.
_HEADER_KEYS = (
    "manifest_schema",
    "object_digest",
    "object_size",
    "generated_at",
    "generator",
    "file_count",
    "total_bytes",
)


class ObjectManifestError(Exception):
    """Raised when a manifest cannot be generated, read or verified."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One file listed in an object manifest.

    Paths are always POSIX-style and relative to the object's own source root.
    """

    path: PurePosixPath
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class ObjectManifest:
    """The parsed contents of an object manifest file."""

    object_digest: str
    object_size: int
    generated_at: str
    generator: str
    entries: tuple[ManifestEntry, ...]

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)


# --------------------------------------------------------------------- layout


def manifest_relative_path(object_digest: str) -> PurePosixPath:
    """Return the manifest's path relative to the Vault root.

    Uses the same fan-out as ``objects/sha256/<aa>/<bb>/<hex>`` but under a
    sibling ``manifests/`` root, so the promise that ``objects/`` is untouched
    once ingested still holds.
    """
    hex_digest = _require_sha256(object_digest)
    return PurePosixPath(
        "manifests", "sha256", hex_digest[:2], hex_digest[2:4], hex_digest
    )


def manifest_path(vault_root: Path, object_digest: str) -> Path:
    """Return the absolute manifest path under one Vault root."""
    return vault_root.expanduser().resolve() / manifest_relative_path(
        object_digest
    ).as_posix()


def manifest_sidecar_path(manifest_file: Path) -> Path:
    """Return the sidecar file that carries the manifest's own digest."""
    return manifest_file.with_name(manifest_file.name + ".sha256")


# ------------------------------------------------------------- serialization


def format_manifest(manifest: ObjectManifest) -> bytes:
    """Serialize a manifest into its canonical, deterministic byte form.

    The layout is a plain-text header followed by a blank line and one line
    per file in the ``sha256sum -c`` format. Entries are sorted by path so the
    same object always produces the same bytes, on any host, at any time.
    """
    _require_sha256(manifest.object_digest)
    header: list[str] = []
    header.append(f"manifest_schema:{MANIFEST_SCHEMA}")
    header.append(f"object_digest:sha256:{_hex(manifest.object_digest)}")
    header.append(f"object_size:{manifest.object_size}")
    header.append(f"generated_at:{manifest.generated_at}")
    header.append(f"generator:{manifest.generator}")
    header.append(f"file_count:{manifest.file_count}")
    header.append(f"total_bytes:{manifest.total_bytes}")

    body: list[str] = []
    seen: set[str] = set()
    for entry in sorted(manifest.entries, key=lambda item: item.path.as_posix()):
        as_posix = entry.path.as_posix()
        if as_posix in seen:
            raise ObjectManifestError(
                f"Duplicate manifest entry for {as_posix!r}."
            )
        seen.add(as_posix)
        _validate_relative(entry.path)
        _require_sha256(entry.digest)
        body.append(f"{_hex(entry.digest)} {entry.size} {as_posix}")

    text = "\n".join(header) + "\n\n" + "\n".join(body)
    if body:
        text += "\n"
    return text.encode("utf-8")


def parse_manifest(payload: bytes) -> ObjectManifest:
    """Parse the canonical byte form back into an ``ObjectManifest``.

    Malformed input raises ``ObjectManifestError`` with a specific reason;
    callers should not treat a manifest as trustworthy without a matching
    sidecar digest, which ``verify_manifest_integrity`` handles.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObjectManifestError(
            "Manifest is not valid UTF-8 text."
        ) from exc

    if "\n\n" not in text:
        raise ObjectManifestError(
            "Manifest is missing the blank line between header and body."
        )
    header_block, body_block = text.split("\n\n", 1)

    header: dict[str, str] = {}
    for raw_line in header_block.splitlines():
        if ":" not in raw_line:
            raise ObjectManifestError(
                f"Malformed header line: {raw_line!r}"
            )
        key, value = raw_line.split(":", 1)
        if key in header:
            raise ObjectManifestError(f"Duplicate header key: {key!r}")
        header[key] = value

    for key in _HEADER_KEYS:
        if key not in header:
            raise ObjectManifestError(f"Missing header key: {key!r}")

    if header["manifest_schema"] != str(MANIFEST_SCHEMA):
        raise ObjectManifestError(
            f"Unsupported manifest schema: {header['manifest_schema']!r}"
        )

    object_digest = header["object_digest"]
    _require_sha256(object_digest)

    try:
        object_size = int(header["object_size"])
        declared_file_count = int(header["file_count"])
        declared_total_bytes = int(header["total_bytes"])
    except ValueError as exc:
        raise ObjectManifestError(
            f"Numeric header field is not an integer: {exc}"
        ) from exc

    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    previous: str | None = None
    for raw_line in body_block.splitlines():
        if not raw_line:
            continue
        parts = raw_line.split(" ", 2)
        if len(parts) != 3:
            raise ObjectManifestError(
                f"Malformed entry line: {raw_line!r}"
            )
        digest_hex, size_text, path_text = parts
        try:
            entry_size = int(size_text)
        except ValueError as exc:
            raise ObjectManifestError(
                f"Entry size is not an integer: {size_text!r}"
            ) from exc
        if not _DIGEST_RE.match(digest_hex):
            raise ObjectManifestError(
                f"Entry digest is not a lowercase sha256: {digest_hex!r}"
            )
        path = PurePosixPath(path_text)
        _validate_relative(path)
        as_posix = path.as_posix()
        if as_posix in seen:
            raise ObjectManifestError(
                f"Duplicate manifest entry: {as_posix!r}"
            )
        if previous is not None and as_posix < previous:
            raise ObjectManifestError(
                "Manifest entries are not sorted by path."
            )
        seen.add(as_posix)
        previous = as_posix
        entries.append(
            ManifestEntry(
                path=path,
                digest=f"sha256:{digest_hex}",
                size=entry_size,
            )
        )

    if declared_file_count != len(entries):
        raise ObjectManifestError(
            "Header file_count disagrees with the number of body entries."
        )

    manifest = ObjectManifest(
        object_digest=object_digest,
        object_size=object_size,
        generated_at=header["generated_at"],
        generator=header["generator"],
        entries=tuple(entries),
    )
    if declared_total_bytes != manifest.total_bytes:
        raise ObjectManifestError(
            "Header total_bytes disagrees with the sum of entry sizes."
        )
    return manifest


# ------------------------------------------------------------- integrity


def compute_sidecar_digest(manifest_bytes: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of the manifest payload."""
    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def verify_manifest_integrity(
    manifest_bytes: bytes, sidecar_bytes: bytes
) -> None:
    """Fail loudly if the sidecar does not match the manifest payload.

    Callers should invoke this before trusting anything parsed out of the
    manifest, so silent corruption of the manifest itself is caught before it
    can hide corruption of the payload it describes.
    """
    expected_line = sidecar_bytes.decode("utf-8", "strict").strip()
    if not expected_line:
        raise ObjectManifestError("Manifest sidecar is empty.")
    # sha256sum-compatible sidecars store "<hex>  <name>", but a plain hex
    # digest is also accepted so the file remains readable to a human.
    hex_expected = expected_line.split()[0]
    if not _DIGEST_RE.match(hex_expected):
        raise ObjectManifestError(
            "Manifest sidecar does not contain a lowercase sha256 digest."
        )
    actual_hex = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_hex != hex_expected:
        raise ObjectManifestError(
            "Manifest bytes do not match the sidecar digest: "
            "the manifest itself is corrupted."
        )


# ----------------------------------------------------------- generation


def generate_object_manifest(
    *,
    archive: Path,
    archive_format: str,
    source_root: str,
    object_digest: str,
    object_size: int,
    generator: str | None = None,
    now: datetime | None = None,
) -> ObjectManifest:
    """Compute the manifest of a preserved archive.

    The archive is extracted to a temporary directory, every regular file is
    hashed, and the result is returned as an in-memory ``ObjectManifest``.
    The temporary directory is torn down before the function returns.

    ``source_root`` names the single top-level directory the archive must
    contain; entries in the manifest are keyed relative to it.
    """
    _require_sha256(object_digest)
    if source_root and ("/" in source_root or source_root in {".", ".."}):
        raise ObjectManifestError(
            f"Invalid source_root: {source_root!r}"
        )

    entries: list[ManifestEntry] = []
    with tempfile.TemporaryDirectory(prefix=".ogv-manifest-") as workspace:
        work = Path(workspace)
        _extract_archive(
            archive=archive,
            destination=work,
            expected_root=source_root,
            archive_format=archive_format,
        )
        root = work / source_root if source_root else work
        if root.is_symlink() or not root.is_dir():
            raise ObjectManifestError(
                f"Archive does not contain the expected root {source_root!r}."
            )
        entries.extend(_hash_tree(root))

    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    generated_at = stamp.isoformat().replace("+00:00", "Z")

    return ObjectManifest(
        object_digest=object_digest,
        object_size=object_size,
        generated_at=generated_at,
        generator=generator or f"offline-game-vault/{__version__}",
        entries=tuple(entries),
    )


def write_manifest_atomically(
    manifest: ObjectManifest, destination: Path
) -> tuple[Path, Path]:
    """Write a manifest and its sidecar next to each other, atomically.

    Returns the paths of the two files written. If the destination already
    contains a manifest whose bytes match the new manifest, both files are
    left untouched and the existing paths are returned.
    """
    payload = format_manifest(manifest)
    sidecar_payload = compute_sidecar_digest(payload).removeprefix("sha256:")
    sidecar_line = f"{sidecar_payload}  {destination.name}\n".encode("utf-8")
    sidecar = manifest_sidecar_path(destination)

    if destination.is_file() and sidecar.is_file():
        try:
            existing_manifest = destination.read_bytes()
            existing_sidecar = sidecar.read_bytes()
        except OSError:
            existing_manifest = b""
            existing_sidecar = b""
        if existing_manifest == payload and existing_sidecar == sidecar_line:
            return destination, sidecar

    destination.parent.mkdir(parents=True, exist_ok=True)

    tmp_manifest = destination.with_name(destination.name + ".incoming")
    tmp_sidecar = sidecar.with_name(sidecar.name + ".incoming")

    try:
        tmp_manifest.write_bytes(payload)
        os.chmod(tmp_manifest, 0o644)
        tmp_sidecar.write_bytes(sidecar_line)
        os.chmod(tmp_sidecar, 0o644)
        # Publish the sidecar first, then the manifest: a reader that sees
        # the manifest can rely on the sidecar being present.
        os.replace(tmp_sidecar, sidecar)
        os.replace(tmp_manifest, destination)
    except OSError as exc:
        for leftover in (tmp_manifest, tmp_sidecar):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise ObjectManifestError(
            f"Could not publish the object manifest: {exc}"
        ) from exc

    return destination, sidecar


def read_manifest(manifest_file: Path) -> ObjectManifest:
    """Read a manifest from disk, checking its sidecar before parsing it."""
    sidecar = manifest_sidecar_path(manifest_file)
    try:
        payload = manifest_file.read_bytes()
        sidecar_payload = sidecar.read_bytes()
    except FileNotFoundError as exc:
        raise ObjectManifestError(
            f"Manifest or sidecar is missing: {exc}"
        ) from exc
    verify_manifest_integrity(payload, sidecar_payload)
    return parse_manifest(payload)


# ------------------------------------------------------------- helpers


def _hex(digest: str) -> str:
    return _require_sha256(digest)


def _require_sha256(digest: str) -> str:
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ObjectManifestError(
            f"Digest must be 'sha256:<hex>': {digest!r}"
        )
    hex_part = digest.removeprefix("sha256:")
    if not _DIGEST_RE.match(hex_part):
        raise ObjectManifestError(
            f"Digest must use lowercase 64-hex form: {digest!r}"
        )
    return hex_part


def _validate_relative(path: PurePosixPath) -> None:
    if path.is_absolute():
        raise ObjectManifestError(f"Path is absolute: {path.as_posix()!r}")
    for part in path.parts:
        if part in {"", ".", ".."}:
            raise ObjectManifestError(
                f"Path has an unsafe component: {path.as_posix()!r}"
            )


def _hash_tree(root: Path) -> Iterable[ManifestEntry]:
    """Walk a tree in sorted order and hash every regular file.

    Symlinks, directories, and special files are omitted deliberately: the
    manifest describes the *contents* of preserved files. Symlinks are
    covered separately by the symlink manifests some contracts declare, and
    that concern belongs to a different layer than per-object content.
    """
    root_resolved = root.resolve(strict=True)
    hashed: list[ManifestEntry] = []
    for absolute in sorted(_walk(root_resolved)):
        try:
            relative = absolute.relative_to(root_resolved)
        except ValueError as exc:
            raise ObjectManifestError(
                f"File escaped the extraction root: {absolute}"
            ) from exc
        info = absolute.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        digest = "sha256:" + _sha256_file(absolute)
        hashed.append(
            ManifestEntry(
                path=PurePosixPath(relative.as_posix()),
                digest=digest,
                size=info.st_size,
            )
        )
    return hashed


def detect_source_root(archive: Path, archive_format: str) -> str:
    """Return the single top-level directory shared by every archive member.

    Preserved objects follow the convention that everything under an archive
    lives inside one named directory. Detecting it removes the burden of
    passing ``source_root`` from callers that already know the archive is
    well-formed; a caller that wants to enforce a specific value can compare
    the returned string to their expectation.
    """
    fmt = archive_format.lower().strip()
    if fmt in {"tar", "tar.gz", "tgz"}:
        names = _tar_member_names(archive, zst=False)
    elif fmt in {"tar.zst", "tzst"}:
        names = _tar_member_names(archive, zst=True)
    elif fmt == "zip":
        try:
            with zipfile.ZipFile(archive) as handle:
                names = handle.namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise ObjectManifestError(
                f"Could not read zip archive: {exc}"
            ) from exc
    else:
        raise ObjectManifestError(
            f"Unsupported archive format: {archive_format!r}"
        )

    roots: set[str] = set()
    for name in names:
        cleaned = name.strip("/").split("/", 1)[0]
        if not cleaned or cleaned in {".", ".."}:
            continue
        roots.add(cleaned)
    if not roots:
        raise ObjectManifestError(
            "Archive has no usable entries."
        )
    if len(roots) == 1:
        root = next(iter(roots))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", root):
            raise ObjectManifestError(
                f"Top-level directory is not a portable identifier: {root!r}"
            )
        return root
    # More than one top-level directory: legitimate for composite objects
    # (a runner plus its ingestion evidence, for instance). The manifest
    # then keys entries from the extraction root itself, so every root
    # directory appears as the first component of the recorded paths.
    return ""


def _tar_member_names(archive: Path, *, zst: bool) -> list[str]:
    if not zst:
        try:
            with tarfile.open(archive, mode="r:*") as handle:
                return handle.getnames()
        except (OSError, tarfile.TarError) as exc:
            raise ObjectManifestError(
                f"Could not read tar archive: {exc}"
            ) from exc
    with tempfile.TemporaryDirectory(prefix=".ogv-manifest-probe-") as tmp:
        decompressed = Path(tmp) / "probe.tar"
        try:
            with decompressed.open("wb") as handle:
                subprocess.run(
                    ["zstd", "-d", "--stdout", str(archive)],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    check=True,
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ObjectManifestError(
                f"Could not decompress zst archive: {exc}"
            ) from exc
        try:
            with tarfile.open(decompressed, mode="r:*") as handle:
                return handle.getnames()
        except (OSError, tarfile.TarError) as exc:
            raise ObjectManifestError(
                f"Could not read tar archive: {exc}"
            ) from exc


def _walk(root: Path) -> Iterable[Path]:
    for entry in sorted(root.iterdir()):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            yield from _walk(entry)
        else:
            yield entry


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------------------- extraction


def _extract_archive(
    *,
    archive: Path,
    destination: Path,
    expected_root: str,
    archive_format: str,
) -> None:
    fmt = archive_format.lower().strip()
    if fmt in {"tar", "tar.gz", "tgz"}:
        _extract_tar(archive, destination, expected_root, zst=False)
    elif fmt in {"tar.zst", "tzst"}:
        _extract_tar(archive, destination, expected_root, zst=True)
    elif fmt == "zip":
        _extract_zip(archive, destination, expected_root)
    else:
        raise ObjectManifestError(
            f"Unsupported archive format: {archive_format!r}"
        )


def _extract_tar(
    archive: Path,
    destination: Path,
    expected_root: str,
    *,
    zst: bool,
) -> None:
    if zst:
        # Reuse the system zstd binary the rest of the core already relies on.
        decompressed = destination / ".incoming.tar"
        try:
            with decompressed.open("wb") as handle:
                subprocess.run(
                    ["zstd", "-d", "--stdout", str(archive)],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    check=True,
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ObjectManifestError(
                f"Could not decompress zst archive: {exc}"
            ) from exc
        source = decompressed
    else:
        source = archive

    try:
        with tarfile.open(source, mode="r:*") as handle:
            members = handle.getmembers()
            if not members:
                raise ObjectManifestError("Archive is empty.")
            usable: list[tarfile.TarInfo] = []
            for member in members:
                path = PurePosixPath(member.name)
                _validate_relative(path)
                if member.ischr() or member.isblk() or member.isfifo():
                    raise ObjectManifestError(
                        "Archive contains a special file."
                    )
                # Symlinks contribute nothing to a content manifest and pose
                # extraction risks (absolute link targets, escapes). Omitting
                # them here is coherent with ``_hash_tree`` also skipping them.
                if member.issym() or member.islnk():
                    continue
                usable.append(member)
            handle.extractall(
                destination,
                members=usable,
                filter="data",
                numeric_owner=False,
            )
    except (OSError, tarfile.TarError) as exc:
        raise ObjectManifestError(
            f"Could not extract archive: {exc}"
        ) from exc
    finally:
        if zst:
            try:
                (destination / ".incoming.tar").unlink()
            except FileNotFoundError:
                pass


def _extract_zip(
    archive: Path, destination: Path, expected_root: str
) -> None:
    try:
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
            if not names:
                raise ObjectManifestError("Archive is empty.")
            for name in names:
                path = PurePosixPath(name)
                _validate_relative(path)
            handle.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ObjectManifestError(
            f"Could not extract archive: {exc}"
        ) from exc
