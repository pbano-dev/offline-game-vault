from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.safe_tar import (
    SafeTarError,
    extract_tar_safely,
)


def add_bytes(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    mode: int = 0o644,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = mode
    archive.addfile(member, io.BytesIO(payload))


class SafeTarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.tar.gz"
        self.destination = self.root / "extracted"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_archive(self, callback) -> None:
        with tarfile.open(self.archive, "w:gz") as archive:
            callback(archive)

    def test_extracts_files_directories_and_exec_mode(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            directory = tarfile.TarInfo("bin")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            add_bytes(
                archive,
                "bin/tool",
                b"#!/bin/sh\nexit 0\n",
                mode=0o755,
            )

        self._write_archive(build)
        result = extract_tar_safely(
            archive_path=self.archive,
            destination=self.destination,
        )

        self.assertEqual(result.regular_file_count, 1)
        self.assertEqual(result.directory_count, 1)
        self.assertEqual(
            (self.destination / "bin/tool").read_bytes(),
            b"#!/bin/sh\nexit 0\n",
        )
        self.assertTrue(
            (self.destination / "bin/tool").stat().st_mode & 0o100
        )

    @unittest.skipIf(
        os.name == "nt",
        "Symlink creation is not reliably available on Windows CI.",
    )
    def test_preserves_safe_relative_symlink(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add_bytes(archive, "lib/real.so", b"library")
            link = tarfile.TarInfo("bin/library.so")
            link.type = tarfile.SYMTYPE
            link.linkname = "../lib/real.so"
            archive.addfile(link)

        self._write_archive(build)
        result = extract_tar_safely(
            archive_path=self.archive,
            destination=self.destination,
        )

        link = self.destination / "bin/library.so"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../lib/real.so")
        self.assertEqual(link.read_bytes(), b"library")
        self.assertEqual(result.symlink_count, 1)

    def test_preserves_safe_hardlink(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add_bytes(archive, "data/original", b"same inode")
            link = tarfile.TarInfo("data/copy")
            link.type = tarfile.LNKTYPE
            link.linkname = "data/original"
            archive.addfile(link)

        self._write_archive(build)
        result = extract_tar_safely(
            archive_path=self.archive,
            destination=self.destination,
        )

        original = self.destination / "data/original"
        copy = self.destination / "data/copy"
        self.assertEqual(original.read_bytes(), copy.read_bytes())
        self.assertEqual(original.stat().st_ino, copy.stat().st_ino)
        self.assertEqual(result.hardlink_count, 1)

    def test_rejects_parent_traversal(self) -> None:
        self._write_archive(
            lambda archive: add_bytes(
                archive,
                "../outside",
                b"escape",
            )
        )

        with self.assertRaisesRegex(
            SafeTarError,
            "path traversal",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

        self.assertFalse((self.root / "outside").exists())

    def test_rejects_absolute_member(self) -> None:
        self._write_archive(
            lambda archive: add_bytes(
                archive,
                "/absolute",
                b"escape",
            )
        )

        with self.assertRaisesRegex(
            SafeTarError,
            "Absolute tar member",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_backslash_member(self) -> None:
        self._write_archive(
            lambda archive: add_bytes(
                archive,
                r"..\outside",
                b"escape",
            )
        )

        with self.assertRaisesRegex(
            SafeTarError,
            "backslash",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_escaping_symlink(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            archive.addfile(link)

        self._write_archive(build)

        with self.assertRaisesRegex(
            SafeTarError,
            "escapes extraction root",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_absolute_symlink(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)

        self._write_archive(build)

        with self.assertRaisesRegex(
            SafeTarError,
            "Absolute symbolic-link target",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_duplicate_member(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add_bytes(archive, "duplicate", b"first")
            add_bytes(archive, "duplicate", b"second")

        self._write_archive(build)

        with self.assertRaisesRegex(
            SafeTarError,
            "Duplicate tar member",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_special_file(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            fifo = tarfile.TarInfo("fifo")
            fifo.type = tarfile.FIFOTYPE
            archive.addfile(fifo)

        self._write_archive(build)

        with self.assertRaisesRegex(
            SafeTarError,
            "Unsupported tar member type",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )

    def test_rejects_existing_destination(self) -> None:
        self.destination.mkdir()
        self._write_archive(
            lambda archive: add_bytes(
                archive,
                "file",
                b"data",
            )
        )

        with self.assertRaisesRegex(
            SafeTarError,
            "already exists",
        ):
            extract_tar_safely(
                archive_path=self.archive,
                destination=self.destination,
            )


if __name__ == "__main__":
    unittest.main()
