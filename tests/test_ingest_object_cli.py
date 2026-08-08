from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.cli import main
from offline_game_vault.object_manifest import read_manifest


class IngestObjectCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"CLI ingest object\n")
        self.digest = (
            "sha256:"
            + hashlib.sha256(self.source.read_bytes()).hexdigest()
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _prepare_capsule_ingest(self) -> None:
        """Build a real tar.gz archive and a matching capsule.json.

        Tests reuse ``self.archive``, ``self.archive_digest``,
        ``self.archive_size`` and ``self.capsule`` in both capsule and
        direct mode so the manifest step has a legitimate archive to hash.
        """
        contents_dir = self.root / "payload"
        contents_dir.mkdir()
        (contents_dir / "file1.txt").write_bytes(b"hello\n")
        (contents_dir / "file2.txt").write_bytes(b"world\n")
        self.archive = self.root / "payload.tar.gz"
        with tarfile.open(self.archive, "w:gz") as handle:
            handle.add(contents_dir, arcname="payload")
        self.archive_digest = (
            "sha256:"
            + hashlib.sha256(self.archive.read_bytes()).hexdigest()
        )
        self.archive_size = self.archive.stat().st_size
        self.capsule = self.root / "capsule.json"
        hex_digest = self.archive_digest.removeprefix("sha256:")
        self.capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "test-capsule",
                    "objects": [
                        {
                            "id": "payload",
                            "archive_path": (
                                f"objects/sha256/{hex_digest[:2]}/"
                                f"{hex_digest[2:4]}/{hex_digest}"
                            ),
                            "digest": self.archive_digest,
                            "size": self.archive_size,
                            "format": "tar.gz",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    # ---------------------------------------------- existing behaviour

    def test_direct_ingest_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.source),
                    "--vault-root",
                    str(self.vault),
                    "--digest",
                    self.digest,
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "ingested")
        self.assertTrue(result["destination_verified"])

    def test_missing_mode_returns_two(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.source),
                    "--vault-root",
                    str(self.vault),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("Provide capsule mode", stderr.getvalue())

    # ---------------------------------------------- manifest hookup

    def test_capsule_ingest_generates_manifest_by_default(self) -> None:
        self._prepare_capsule_ingest()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--capsule",
                    str(self.capsule),
                    "--object-id",
                    "payload",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ingested")
        self.assertTrue(payload["manifest_generated"])
        self.assertFalse(payload["manifest_already_present"])
        self.assertFalse(payload["manifest_skipped"])
        self.assertIsNone(payload["manifest_skipped_reason"])
        self.assertIsNone(payload["manifest_warning"])
        self.assertIsNotNone(payload["manifest_path"])

        # read_manifest verifies the sidecar and parses the payload; both
        # must agree with the object we just ingested.
        manifest_file = Path(payload["manifest_path"])
        sidecar = manifest_file.with_name(manifest_file.name + ".sha256")
        self.assertTrue(manifest_file.is_file())
        self.assertTrue(sidecar.is_file())
        parsed = read_manifest(manifest_file)
        self.assertEqual(parsed.object_digest, self.archive_digest)
        self.assertEqual(parsed.object_size, self.archive_size)
        self.assertGreater(parsed.file_count, 0)

    def test_no_manifest_flag_skips_generation(self) -> None:
        self._prepare_capsule_ingest()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--capsule",
                    str(self.capsule),
                    "--object-id",
                    "payload",
                    "--no-manifest",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["manifest_generated"])
        self.assertTrue(payload["manifest_skipped"])
        self.assertEqual(
            payload["manifest_skipped_reason"], "disabled by --no-manifest"
        )
        self.assertIsNone(payload["manifest_warning"])
        # The path is reported (it names the canonical location) but the
        # file is not on disk.
        self.assertIsNotNone(payload["manifest_path"])
        self.assertFalse(Path(payload["manifest_path"]).exists())

    def test_manifest_already_present_is_not_regenerated(self) -> None:
        self._prepare_capsule_ingest()

        stdout_first = io.StringIO()
        with contextlib.redirect_stdout(stdout_first):
            code_first = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--capsule",
                    str(self.capsule),
                    "--object-id",
                    "payload",
                    "--json",
                ]
            )
        self.assertEqual(code_first, 0)
        first = json.loads(stdout_first.getvalue())
        self.assertTrue(first["manifest_generated"])
        manifest_file = Path(first["manifest_path"])
        first_mtime = manifest_file.stat().st_mtime_ns

        stdout_second = io.StringIO()
        with contextlib.redirect_stdout(stdout_second):
            code_second = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--capsule",
                    str(self.capsule),
                    "--object-id",
                    "payload",
                    "--json",
                ]
            )
        self.assertEqual(code_second, 0)
        second = json.loads(stdout_second.getvalue())
        self.assertFalse(second["manifest_generated"])
        self.assertTrue(second["manifest_already_present"])
        self.assertIsNone(second["manifest_warning"])
        # The manifest file was not rewritten.
        self.assertEqual(manifest_file.stat().st_mtime_ns, first_mtime)

    def test_manifest_failure_does_not_invalidate_ingest(self) -> None:
        # Direct mode: source is arbitrary bytes but --format claims tar.gz.
        # The ingest succeeds (any bytes are content-addressable) and the
        # manifest step fails because the bytes are not a valid tar.gz.
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.source),
                    "--vault-root",
                    str(self.vault),
                    "--digest",
                    self.digest,
                    "--format",
                    "tar.gz",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ingested")
        self.assertTrue(payload["destination_verified"])
        self.assertFalse(payload["manifest_generated"])
        self.assertFalse(payload["manifest_already_present"])
        self.assertFalse(payload["manifest_skipped"])
        self.assertIsNotNone(payload["manifest_warning"])
        self.assertIn("warning", stderr.getvalue().lower())

    def test_direct_mode_without_format_skips_manifest(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.source),
                    "--vault-root",
                    str(self.vault),
                    "--digest",
                    self.digest,
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["manifest_skipped"])
        self.assertEqual(
            payload["manifest_skipped_reason"],
            "direct mode without --format",
        )
        self.assertIsNone(payload["manifest_warning"])

    def test_direct_mode_with_format_generates_and_reads_manifest(
        self,
    ) -> None:
        self._prepare_capsule_ingest()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--digest",
                    self.archive_digest,
                    "--format",
                    "tar.gz",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["manifest_generated"])
        self.assertIsNone(payload["manifest_warning"])
        manifest_file = Path(payload["manifest_path"])
        self.assertTrue(manifest_file.is_file())
        parsed = read_manifest(manifest_file)
        self.assertEqual(parsed.object_digest, self.archive_digest)
        self.assertGreater(parsed.file_count, 0)

    def test_capsule_format_conflict_aborts_ingest(self) -> None:
        self._prepare_capsule_ingest()  # capsule declares tar.gz
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = main(
                [
                    "ingest-object",
                    "--source",
                    str(self.archive),
                    "--vault-root",
                    str(self.vault),
                    "--capsule",
                    str(self.capsule),
                    "--object-id",
                    "payload",
                    "--format",
                    "tar.zst",
                    "--json",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("contradicts", stderr.getvalue())
        # The object was NOT ingested — the canonical destination is empty.
        hex_digest = self.archive_digest.removeprefix("sha256:")
        destination = (
            self.vault
            / "objects"
            / "sha256"
            / hex_digest[:2]
            / hex_digest[2:4]
            / hex_digest
        )
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
