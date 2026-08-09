"""Unit tests for the standalone manifest-based verification helper.

Tests exercise the "based on existence" contract: hard failures are
receipt sidecar corruption, generated-files mismatches, and manifest
sidecar corruption. Extras and unmatched manifest entries are
informational, not failures.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.portable_manifest_check import (
    ManifestCheckError,
    verify_by_manifests,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _FixtureBuilder:
    """Assemble a minimal materialization directory for verification tests.

    Layout produced:

        <root>/
          receipt.json                    (primary receipt)
          receipt.json.sha256             (its sidecar)
          JUGAR.sh                        (generated)
          metadata/generated-files.json   (lists JUGAR.sh + hashes)
          metadata/manifests/sha256/<aa>/<bb>/<hex>          (per-object)
          metadata/manifests/sha256/<aa>/<bb>/<hex>.sha256   (its sidecar)
          prefix/data.bin                 (object content, hashed in manifest)
    """

    def __init__(self, root: Path):
        self.root = root
        self.receipt_path = root / "receipt.json"
        self.generated_files: dict[str, bytes] = {}
        self.object_files: dict[str, bytes] = {}
        self.object_digest = "sha256:" + "0" * 63 + "a"

    def add_generated(self, relative_path: str, payload: bytes) -> None:
        self.generated_files[relative_path] = payload

    def add_object_file(self, path_in_object: str, payload: bytes) -> None:
        self.object_files[path_in_object] = payload

    def build(self) -> None:
        # 1. Primary receipt + sidecar.
        payload = json.dumps(
            {"schema": 0, "receipt_id": "test"}, sort_keys=True
        ).encode() + b"\n"
        self.receipt_path.write_bytes(payload)
        receipt_hex = _sha256(payload)
        self.receipt_path.with_name(
            self.receipt_path.name + ".sha256"
        ).write_text(
            f"{receipt_hex}  {self.receipt_path.name}\n",
            encoding="utf-8",
        )

        # 2. Generated files + their manifest.
        entries = []
        for relative, data in self.generated_files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256(data),
                    "bytes": len(data),
                }
            )
        generated_manifest = self.root / "metadata/generated-files.json"
        generated_manifest.parent.mkdir(parents=True, exist_ok=True)
        generated_manifest.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "generator": "test",
                    "files": entries,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # 3. Object content under a synthetic prefix.
        for path_in_object, data in self.object_files.items():
            target = self.root / "prefix" / path_in_object
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        # 4. Per-object manifest in canonical format.
        hex_digest = self.object_digest.removeprefix("sha256:")
        manifest_path = (
            self.root
            / "metadata/manifests/sha256"
            / hex_digest[:2]
            / hex_digest[2:4]
            / hex_digest
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        header_lines = [
            "manifest_schema:0",
            f"object_digest:{self.object_digest}",
            "object_size:0",
            "generated_at:2026-01-01T00:00:00Z",
            "generator:test",
            f"file_count:{len(self.object_files)}",
            f"total_bytes:{sum(len(d) for d in self.object_files.values())}",
        ]
        body_lines = [
            f"{_sha256(data)} {len(data)} {path_in_object}"
            for path_in_object, data in self.object_files.items()
        ]
        text = "\n".join(header_lines) + "\n\n" + "\n".join(body_lines)
        if body_lines:
            text += "\n"
        manifest_body = text.encode("utf-8")
        manifest_path.write_bytes(manifest_body)
        manifest_hex = _sha256(manifest_body)
        manifest_path.with_name(
            manifest_path.name + ".sha256"
        ).write_text(
            f"{manifest_hex}  {manifest_path.name}\n",
            encoding="utf-8",
        )
        self.manifest_path = manifest_path


class ManifestCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.fixture = _FixtureBuilder(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _build_valid_fixture(self) -> None:
        self.fixture.add_generated("JUGAR.sh", b"#!/bin/sh\nexit 0\n")
        self.fixture.add_object_file("data.bin", b"payload content\n")
        self.fixture.add_object_file("nested/other.bin", b"another\n")
        self.fixture.build()

    # -------------------------------------------------- happy path

    def test_verify_by_manifests_happy_path(self) -> None:
        self._build_valid_fixture()
        report = verify_by_manifests(
            root=self.root,
            receipt_path=self.fixture.receipt_path,
        )
        self.assertTrue(report["receipt_verified"])
        self.assertEqual(report["generated_files_verified"], 1)
        self.assertEqual(report["generated_files_failed"], [])
        self.assertEqual(report["manifests_verified"], 1)
        self.assertEqual(report["manifests_corrupted"], [])
        self.assertEqual(report["object_files_matched"], 2)
        self.assertEqual(report["object_files_declared"], 2)
        self.assertEqual(
            report["object_files_not_found_at_destination"], 0
        )

    # ----------------------------------------------- hard failures

    def test_receipt_sidecar_mismatch_fails_fast(self) -> None:
        self._build_valid_fixture()
        self.fixture.receipt_path.write_bytes(b'{"tampered": true}\n')
        with self.assertRaises(ManifestCheckError) as ctx:
            verify_by_manifests(
                root=self.root,
                receipt_path=self.fixture.receipt_path,
            )
        self.assertIn("sidecar mismatch", str(ctx.exception))

    def test_receipt_sidecar_absent_fails_fast(self) -> None:
        self._build_valid_fixture()
        self.fixture.receipt_path.with_name(
            self.fixture.receipt_path.name + ".sha256"
        ).unlink()
        with self.assertRaises(ManifestCheckError):
            verify_by_manifests(
                root=self.root,
                receipt_path=self.fixture.receipt_path,
            )

    def test_generated_file_content_mismatch_is_hard_failure(self) -> None:
        self._build_valid_fixture()
        (self.root / "JUGAR.sh").write_bytes(b"#!/bin/sh\nrm -rf /\n")
        with self.assertRaises(ManifestCheckError) as ctx:
            verify_by_manifests(
                root=self.root,
                receipt_path=self.fixture.receipt_path,
            )
        self.assertIn("Generated file digest mismatch", str(ctx.exception))

    def test_generated_file_missing_is_hard_failure(self) -> None:
        self._build_valid_fixture()
        (self.root / "JUGAR.sh").unlink()
        with self.assertRaises(ManifestCheckError) as ctx:
            verify_by_manifests(
                root=self.root,
                receipt_path=self.fixture.receipt_path,
            )
        self.assertIn("absent", str(ctx.exception))

    def test_manifest_sidecar_mismatch_is_hard_failure(self) -> None:
        self._build_valid_fixture()
        sidecar = self.fixture.manifest_path.with_name(
            self.fixture.manifest_path.name + ".sha256"
        )
        sidecar.write_text(
            f"{'0'*64}  {self.fixture.manifest_path.name}\n",
            encoding="utf-8",
        )
        with self.assertRaises(ManifestCheckError) as ctx:
            verify_by_manifests(
                root=self.root,
                receipt_path=self.fixture.receipt_path,
            )
        self.assertIn("Manifest sidecar mismatch", str(ctx.exception))

    # --------------------------------------- informational, not failure

    def test_missing_object_content_is_informational(self) -> None:
        """Manifest entries not present at destination do NOT fail
        verification; they show up as an informational count.
        Materializations legitimately discard object content (e.g. UMU
        keeps only parts of runner and runtime archives)."""
        self._build_valid_fixture()
        (self.root / "prefix/nested/other.bin").unlink()
        report = verify_by_manifests(
            root=self.root,
            receipt_path=self.fixture.receipt_path,
        )
        self.assertTrue(report["receipt_verified"])
        self.assertEqual(report["object_files_matched"], 1)
        self.assertEqual(
            report["object_files_not_found_at_destination"], 1
        )

    def test_unexpected_content_is_ignored(self) -> None:
        """Extra files anywhere in the tree do NOT fail verification.
        This is the 'based on existence' principle: user-added notes,
        composition extras inside object subtrees, etc. are welcome."""
        self._build_valid_fixture()
        (self.root / "prefix/nested/rogue.bin").write_bytes(b"i am new\n")
        (self.root / "user-notes.md").write_bytes(b"my thoughts\n")
        report = verify_by_manifests(
            root=self.root,
            receipt_path=self.fixture.receipt_path,
        )
        self.assertTrue(report["receipt_verified"])
        self.assertEqual(report["object_files_matched"], 2)


if __name__ == "__main__":
    unittest.main()
