"""Object manifests describe what one preserved archive contains.

Their purpose is to let a materialization verify itself with nothing but the
files it ships, even years after the Vault that produced it is gone. The
properties these tests fix are the properties that guarantee that promise.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault.object_manifest import (
    ManifestEntry,
    ObjectManifest,
    ObjectManifestError,
    compute_sidecar_digest,
    format_manifest,
    generate_object_manifest,
    manifest_path,
    manifest_relative_path,
    manifest_sidecar_path,
    parse_manifest,
    read_manifest,
    verify_manifest_integrity,
    write_manifest_atomically,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _tar_gz(
    destination: Path,
    root: str,
    entries: dict[str, bytes],
) -> None:
    """Write a small tar.gz whose contents mimic a preserved archive."""
    directories: set[str] = {root}
    for name in entries:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]))
    with tarfile.open(destination, "w:gz") as archive:
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))


class LayoutTest(unittest.TestCase):
    """The location of a manifest is a pure function of its object digest."""

    def test_manifest_paths_fan_out_like_objects(self) -> None:
        rel = manifest_relative_path(DIGEST_A)
        self.assertEqual(rel.as_posix(), f"manifests/sha256/aa/aa/{'a' * 64}")

    def test_manifest_path_is_anchored_at_the_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = manifest_path(root, DIGEST_A)
            self.assertTrue(path.is_relative_to(root.resolve()))

    def test_invalid_digests_are_rejected_up_front(self) -> None:
        for bad in ("not-a-digest", "sha256:short", "sha512:" + "a" * 64):
            with self.assertRaises(ObjectManifestError):
                manifest_relative_path(bad)


class SerializationTest(unittest.TestCase):
    """The bytes of a manifest are a stable function of its contents."""

    def _manifest(self, entries=(), when: str = "2026-08-07T00:00:00Z"):
        return ObjectManifest(
            object_digest=DIGEST_A,
            object_size=1024,
            generated_at=when,
            generator="offline-game-vault/test",
            entries=tuple(entries),
        )

    def test_serialization_is_deterministic(self) -> None:
        entries = (
            ManifestEntry(Path("b").as_posix() and __import__("pathlib").PurePosixPath("b"),
                          "sha256:" + "2" * 64, 4),
            ManifestEntry(__import__("pathlib").PurePosixPath("a"),
                          "sha256:" + "1" * 64, 3),
        )
        first = format_manifest(self._manifest(entries=entries))
        second = format_manifest(self._manifest(entries=entries))
        self.assertEqual(first, second)

    def test_entries_are_sorted_by_path(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("b/z"),
                          "sha256:" + "2" * 64, 1),
            ManifestEntry(pathlib.PurePosixPath("a/z"),
                          "sha256:" + "1" * 64, 1),
        )
        payload = format_manifest(self._manifest(entries=entries)).decode()
        lines = [
            line for line in payload.splitlines()
            if line and not line.startswith(("manifest_schema",
                                             "object_digest", "object_size",
                                             "generated_at", "generator",
                                             "file_count", "total_bytes"))
        ]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("a/z"))
        self.assertTrue(lines[1].endswith("b/z"))

    def test_body_carries_digest_size_and_path(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("readme.txt"),
                          "sha256:" + "e" * 64, 12),
        )
        payload = format_manifest(self._manifest(entries=entries)).decode()
        _header, body = payload.split("\n\n", 1)
        self.assertEqual(body.strip(), "e" * 64 + " 12 readme.txt")

    def test_a_manifest_with_a_wrong_total_bytes_is_rejected(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("only"),
                          "sha256:" + "7" * 64, 42),
        )
        payload = format_manifest(self._manifest(entries=entries)).decode()
        broken = payload.replace("total_bytes:42", "total_bytes:9999")
        with self.assertRaises(ObjectManifestError):
            parse_manifest(broken.encode())

    def test_round_trip_preserves_content(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("dir/file"),
                          "sha256:" + "3" * 64, 7),
            ManifestEntry(pathlib.PurePosixPath("other"),
                          "sha256:" + "4" * 64, 11),
        )
        original = self._manifest(entries=entries)
        payload = format_manifest(original)
        parsed = parse_manifest(payload)
        self.assertEqual(parsed.object_digest, original.object_digest)
        self.assertEqual(parsed.file_count, original.file_count)
        self.assertEqual(
            {e.path.as_posix() for e in parsed.entries},
            {e.path.as_posix() for e in original.entries},
        )

    def test_absolute_paths_are_rejected(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("/etc/passwd"),
                          "sha256:" + "5" * 64, 1),
        )
        with self.assertRaises(ObjectManifestError):
            format_manifest(self._manifest(entries=entries))

    def test_parent_traversal_is_rejected(self) -> None:
        import pathlib
        entries = (
            ManifestEntry(pathlib.PurePosixPath("../escape"),
                          "sha256:" + "6" * 64, 1),
        )
        with self.assertRaises(ObjectManifestError):
            format_manifest(self._manifest(entries=entries))


class IntegrityTest(unittest.TestCase):
    """A manifest is only useful if its own integrity is checkable."""

    def test_valid_sidecar_accepts_matching_manifest(self) -> None:
        payload = b"anything\n"
        sidecar = compute_sidecar_digest(payload).removeprefix("sha256:") \
            .encode("utf-8") + b"  file\n"
        verify_manifest_integrity(payload, sidecar)

    def test_mismatched_sidecar_is_rejected(self) -> None:
        payload = b"anything\n"
        wrong = ("0" * 64).encode("utf-8") + b"  file\n"
        with self.assertRaises(ObjectManifestError):
            verify_manifest_integrity(payload, wrong)

    def test_empty_sidecar_is_rejected(self) -> None:
        with self.assertRaises(ObjectManifestError):
            verify_manifest_integrity(b"anything", b"")


class GenerationTest(unittest.TestCase):
    """Generation is a pure function of the archive's bytes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.archive = self.root / "example.tar.gz"
        _tar_gz(
            self.archive,
            "payload",
            {
                "payload/readme.txt": b"hello\n",
                "payload/game/main.exe": b"MZ" + b"\x00" * 30,
                "payload/game/data/level.pak": b"\xff" * 4096,
            },
        )
        self.digest = "sha256:" + hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()

    def _generate(self, when: datetime | None = None) -> ObjectManifest:
        return generate_object_manifest(
            archive=self.archive,
            archive_format="tar.gz",
            source_root="payload",
            object_digest=self.digest,
            object_size=self.archive.stat().st_size,
            generator="offline-game-vault/test",
            now=when,
        )

    def test_manifest_lists_every_regular_file(self) -> None:
        manifest = self._generate()
        paths = {entry.path.as_posix() for entry in manifest.entries}
        self.assertEqual(
            paths,
            {"readme.txt", "game/main.exe", "game/data/level.pak"},
        )

    def test_digests_match_the_extracted_bytes(self) -> None:
        manifest = self._generate()
        wanted = {
            "readme.txt": "sha256:" + hashlib.sha256(b"hello\n").hexdigest(),
            "game/main.exe": "sha256:" + hashlib.sha256(
                b"MZ" + b"\x00" * 30
            ).hexdigest(),
            "game/data/level.pak": "sha256:" + hashlib.sha256(
                b"\xff" * 4096
            ).hexdigest(),
        }
        for entry in manifest.entries:
            self.assertEqual(entry.digest, wanted[entry.path.as_posix()])

    def test_generation_is_deterministic_for_the_same_bytes(self) -> None:
        stamp = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
        first = format_manifest(self._generate(when=stamp))
        second = format_manifest(self._generate(when=stamp))
        self.assertEqual(first, second)

    def test_generation_rejects_a_wrong_source_root(self) -> None:
        with self.assertRaises(ObjectManifestError):
            generate_object_manifest(
                archive=self.archive,
                archive_format="tar.gz",
                source_root="other",
                object_digest=self.digest,
                object_size=self.archive.stat().st_size,
                generator="offline-game-vault/test",
            )

    def test_symlinks_are_omitted_from_the_manifest(self) -> None:
        payload_root = self.root / "with-symlink"
        payload_root.mkdir()
        archive = self.root / "with-symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name in ("linked", "linked/game", "linked/prefix"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            for name, payload in (("linked/game/main.exe", b"exe"),):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
            symlink = tarfile.TarInfo("linked/prefix/z_drive")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../../game"
            tf.addfile(symlink)

        digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest = generate_object_manifest(
            archive=archive,
            archive_format="tar.gz",
            source_root="linked",
            object_digest=digest,
            object_size=archive.stat().st_size,
            generator="offline-game-vault/test",
        )
        paths = {entry.path.as_posix() for entry in manifest.entries}
        self.assertEqual(paths, {"game/main.exe"})

    def test_absolute_symlinks_do_not_break_extraction(self) -> None:
        """steamrt-style archives carry symlinks whose target is absolute.

        The manifest still processes them, because ``_hash_tree`` omits
        symlinks anyway and the extractor now filters them before
        ``extractall`` gets a chance to reject them for safety.
        """
        archive = self.root / "abs-sym.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name in ("payload", "payload/bin"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            info = tarfile.TarInfo("payload/bin/real.exe")
            info.size = 3
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(b"exe"))
            evil = tarfile.TarInfo("payload/bin/getconf")
            evil.type = tarfile.SYMTYPE
            evil.linkname = "/usr/bin/getconf"  # absolute target
            tf.addfile(evil)

        digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest = generate_object_manifest(
            archive=archive,
            archive_format="tar.gz",
            source_root="payload",
            object_digest=digest,
            object_size=archive.stat().st_size,
            generator="offline-game-vault/test",
        )
        paths = {entry.path.as_posix() for entry in manifest.entries}
        self.assertEqual(paths, {"bin/real.exe"})

    def test_archives_with_multiple_top_level_roots_are_accepted(self) -> None:
        """Composite objects (runner + ingestion evidence) have two roots."""
        archive = self.root / "multi-root.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name in ("engine", "engine/inner", "evidence"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            for name, payload in (
                ("engine/inner/binary", b"bin"),
                ("evidence/log.txt", b"ok\n"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))

        digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        from offline_game_vault.object_manifest import detect_source_root
        source_root = detect_source_root(archive, "tar.gz")
        self.assertEqual(source_root, "")  # empty means "extraction root"

        manifest = generate_object_manifest(
            archive=archive,
            archive_format="tar.gz",
            source_root=source_root,
            object_digest=digest,
            object_size=archive.stat().st_size,
            generator="offline-game-vault/test",
        )
        paths = {entry.path.as_posix() for entry in manifest.entries}
        self.assertEqual(paths, {"engine/inner/binary", "evidence/log.txt"})


class WriteAndReadTest(unittest.TestCase):
    """Round-tripping through disk preserves the manifest and its sidecar."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _manifest(self) -> ObjectManifest:
        import pathlib
        return ObjectManifest(
            object_digest=DIGEST_A,
            object_size=64,
            generated_at="2026-08-07T00:00:00Z",
            generator="offline-game-vault/test",
            entries=(
                ManifestEntry(pathlib.PurePosixPath("a"),
                              "sha256:" + "1" * 64, 1),
                ManifestEntry(pathlib.PurePosixPath("b"),
                              "sha256:" + "2" * 64, 2),
            ),
        )

    def test_written_manifest_matches_the_sidecar(self) -> None:
        destination = manifest_path(self.root, DIGEST_A)
        write_manifest_atomically(self._manifest(), destination)
        parsed = read_manifest(destination)
        self.assertEqual(parsed.object_digest, DIGEST_A)
        self.assertEqual(parsed.file_count, 2)

    def test_rewriting_the_same_manifest_is_idempotent(self) -> None:
        destination = manifest_path(self.root, DIGEST_A)
        write_manifest_atomically(self._manifest(), destination)
        original_bytes = destination.read_bytes()
        original_mtime = destination.stat().st_mtime_ns
        write_manifest_atomically(self._manifest(), destination)
        self.assertEqual(destination.read_bytes(), original_bytes)
        self.assertEqual(destination.stat().st_mtime_ns, original_mtime)

    def test_read_manifest_rejects_a_tampered_manifest(self) -> None:
        destination = manifest_path(self.root, DIGEST_A)
        write_manifest_atomically(self._manifest(), destination)
        # Corrupt the manifest without touching its sidecar.
        payload = destination.read_bytes()
        destination.write_bytes(payload + b"# rogue line\n")
        with self.assertRaises(ObjectManifestError):
            read_manifest(destination)

    def test_read_manifest_rejects_a_tampered_sidecar(self) -> None:
        destination = manifest_path(self.root, DIGEST_A)
        write_manifest_atomically(self._manifest(), destination)
        sidecar = manifest_sidecar_path(destination)
        sidecar.write_bytes(b"0" * 64 + b"  " + destination.name.encode()
                            + b"\n")
        with self.assertRaises(ObjectManifestError):
            read_manifest(destination)

    def test_missing_files_raise_a_specific_error(self) -> None:
        destination = manifest_path(self.root, DIGEST_A)
        with self.assertRaises(ObjectManifestError):
            read_manifest(destination)


if __name__ == "__main__":
    unittest.main()
