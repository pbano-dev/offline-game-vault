"""The generate-object-manifest command bridges the module and the operator.

Its job is to accept a small, familiar surface (capsule mode or direct mode),
locate an object, hash its contents, and write the manifest to a canonical or
caller-chosen location. These tests fix that behavior end to end.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault import cli
from offline_game_vault.object_manifest import (
    ObjectManifestError,
    manifest_path,
    manifest_sidecar_path,
    read_manifest,
)


def _sample_archive(destination: Path) -> tuple[Path, str, int]:
    """Build a tiny archive and return its path, sha256: digest and size."""
    with tarfile.open(destination, "w:gz") as tf:
        for name in ("payload", "payload/inner"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tf.addfile(info)
        for name, data in (
            ("payload/readme.txt", b"hola\n"),
            ("payload/inner/x.bin", b"\x00\x01\x02\x03"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    raw = destination.read_bytes()
    return destination, "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)


class DirectModeTest(unittest.TestCase):
    """Direct mode: path, digest, size and format supplied on the CLI."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive, self.digest, self.size = _sample_archive(
            self.root / "sample.tar.gz"
        )
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def _run(self, *extra: str) -> int:
        return cli.main([
            "generate-object-manifest",
            "--path", str(self.archive),
            "--digest", self.digest,
            "--expected-size", str(self.size),
            "--format", "tar.gz",
            "--vault-root", str(self.vault),
            *extra,
        ])

    def test_manifest_is_written_to_the_canonical_vault_location(self) -> None:
        self.assertEqual(self._run(), 0)

        expected = manifest_path(self.vault, self.digest)
        self.assertTrue(expected.is_file(), f"missing: {expected}")
        self.assertTrue(
            manifest_sidecar_path(expected).is_file(),
            "sidecar not written",
        )

        parsed = read_manifest(expected)
        paths = {entry.path.as_posix() for entry in parsed.entries}
        self.assertEqual(paths, {"readme.txt", "inner/x.bin"})

    def test_output_flag_overrides_the_canonical_location(self) -> None:
        target = self.root / "custom" / "my-manifest"
        self.assertEqual(self._run("--output", str(target)), 0)
        self.assertTrue(target.is_file())
        self.assertTrue(manifest_sidecar_path(target).is_file())

    def test_dry_run_writes_nothing_but_still_reports(self) -> None:
        target = self.root / "custom" / "my-manifest"
        rc = self._run("--output", str(target), "--dry-run", "--json")
        self.assertEqual(rc, 0)
        self.assertFalse(target.exists())
        self.assertFalse(manifest_sidecar_path(target).exists())

    def test_json_output_carries_the_essentials(self) -> None:
        import contextlib
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(self._run("--json"), 0)
        payload = json.loads(capture.getvalue())
        self.assertEqual(payload["object_digest"], self.digest)
        self.assertEqual(payload["file_count"], 2)
        self.assertEqual(payload["source_root"], "payload")

    def test_wrong_digest_is_rejected_before_hashing_anything(self) -> None:
        rc = cli.main([
            "generate-object-manifest",
            "--path", str(self.archive),
            "--digest", "sha256:" + "0" * 64,
            "--expected-size", str(self.size),
            "--format", "tar.gz",
            "--vault-root", str(self.vault),
        ])
        self.assertEqual(rc, 2)

    def test_direct_mode_without_format_fails_clearly(self) -> None:
        rc = cli.main([
            "generate-object-manifest",
            "--path", str(self.archive),
            "--digest", self.digest,
            "--vault-root", str(self.vault),
        ])
        self.assertEqual(rc, 2)

    def test_neither_output_nor_vault_root_is_rejected(self) -> None:
        rc = cli.main([
            "generate-object-manifest",
            "--path", str(self.archive),
            "--digest", self.digest,
            "--expected-size", str(self.size),
            "--format", "tar.gz",
        ])
        self.assertEqual(rc, 2)


class CapsuleModeTest(unittest.TestCase):
    """Capsule mode: the format comes from the capsule declaration."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive, self.digest, self.size = _sample_archive(
            self.root / "sample.tar.gz"
        )
        self.vault = self.root / "vault"
        self.vault.mkdir()

        # Import here to avoid depending on storage from module scope.
        from offline_game_vault.storage import (
            ObjectSpec,
            canonical_object_path,
            ingest_object,
        )
        ingest_object(
            source=self.archive,
            destination_spec=ObjectSpec(
                object_id="sample",
                path=canonical_object_path(self.vault, self.digest),
                expected_digest=self.digest,
                expected_size=self.size,
                vault_root=self.vault.resolve(),
            ),
        )

        self.capsule = self.root / "capsule.json"
        raw = self.digest.removeprefix("sha256:")
        self.capsule.write_text(
            json.dumps({
                "schema": 0,
                "capsule_id": "example",
                "objects": [{
                    "id": "sample",
                    "digest": self.digest,
                    "roles": ["game_payload"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": (
                        f"objects/sha256/{raw[:2]}/{raw[2:4]}/{raw}"
                    ),
                    "shared": False,
                    "size": self.size,
                }],
                "profiles": [],
            }),
            encoding="utf-8",
        )

    def test_manifest_is_generated_from_the_capsule_declaration(self) -> None:
        rc = cli.main([
            "generate-object-manifest",
            "--capsule", str(self.capsule),
            "--object-id", "sample",
            "--vault-root", str(self.vault),
        ])
        self.assertEqual(rc, 0)
        expected = manifest_path(self.vault, self.digest)
        self.assertTrue(expected.is_file())
        parsed = read_manifest(expected)
        self.assertEqual(parsed.file_count, 2)

    def test_unknown_object_id_fails_clearly(self) -> None:
        rc = cli.main([
            "generate-object-manifest",
            "--capsule", str(self.capsule),
            "--object-id", "nope",
            "--vault-root", str(self.vault),
        ])
        self.assertEqual(rc, 2)


class IdempotenceTest(unittest.TestCase):
    """Running the command twice on the same object is a no-op."""

    def test_second_run_does_not_rewrite_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, digest, size = _sample_archive(root / "sample.tar.gz")
            vault = root / "vault"
            vault.mkdir()

            argv = [
                "generate-object-manifest",
                "--path", str(archive),
                "--digest", digest,
                "--expected-size", str(size),
                "--format", "tar.gz",
                "--vault-root", str(vault),
            ]
            self.assertEqual(cli.main(argv), 0)
            expected = manifest_path(vault, digest)
            first_mtime = expected.stat().st_mtime_ns
            first_bytes = expected.read_bytes()

            self.assertEqual(cli.main(argv), 0)
            self.assertEqual(expected.read_bytes(), first_bytes)
            self.assertEqual(expected.stat().st_mtime_ns, first_mtime)


if __name__ == "__main__":
    unittest.main()
