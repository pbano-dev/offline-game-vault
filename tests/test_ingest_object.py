from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.storage import (
    IngestError,
    canonical_object_path,
    capsule_destination_spec,
    direct_destination_spec,
    ingest_object,
)


class IngestObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source.bin"
        self.payload = b"content-addressed object\n"
        self.source.write_bytes(self.payload)
        self.digest = (
            "sha256:"
            + hashlib.sha256(self.payload).hexdigest()
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_canonical_path(self) -> None:
        hexadecimal = self.digest.removeprefix("sha256:")
        path = canonical_object_path(self.vault, self.digest)
        self.assertEqual(
            path,
            (
                self.vault.resolve()
                / "objects"
                / "sha256"
                / hexadecimal[:2]
                / hexadecimal[2:4]
                / hexadecimal
            ),
        )

    def test_ingests_and_verifies_destination(self) -> None:
        spec = direct_destination_spec(
            vault_root=self.vault,
            digest=self.digest,
            expected_size=len(self.payload),
        )

        result = ingest_object(
            source=self.source,
            destination_spec=spec,
        )

        self.assertEqual(result.status, "ingested")
        self.assertTrue(result.copied)
        self.assertTrue(result.source_verified)
        self.assertTrue(result.destination_verified)
        self.assertEqual(spec.path.read_bytes(), self.payload)
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_existing_matching_object_is_not_copied(self) -> None:
        spec = direct_destination_spec(
            vault_root=self.vault,
            digest=self.digest,
        )
        spec.path.parent.mkdir(parents=True)
        spec.path.write_bytes(self.payload)

        result = ingest_object(
            source=self.source,
            destination_spec=spec,
        )

        self.assertEqual(result.status, "already_present")
        self.assertFalse(result.copied)
        self.assertTrue(result.destination_verified)

    def test_existing_conflict_is_never_overwritten(self) -> None:
        spec = direct_destination_spec(
            vault_root=self.vault,
            digest=self.digest,
        )
        spec.path.parent.mkdir(parents=True)
        conflict = b"conflicting existing object\n"
        spec.path.write_bytes(conflict)

        with self.assertRaisesRegex(
            IngestError,
            "refusing to overwrite",
        ):
            ingest_object(
                source=self.source,
                destination_spec=spec,
            )

        self.assertEqual(spec.path.read_bytes(), conflict)

    def test_source_digest_mismatch_leaves_no_object(self) -> None:
        wrong_digest = "sha256:" + ("0" * 64)
        spec = direct_destination_spec(
            vault_root=self.vault,
            digest=wrong_digest,
        )

        with self.assertRaisesRegex(
            IngestError,
            "Source digest mismatch",
        ):
            ingest_object(
                source=self.source,
                destination_spec=spec,
            )

        self.assertFalse(spec.path.exists())
        if spec.path.parent.exists():
            self.assertEqual(
                list(spec.path.parent.glob(".incoming-*")),
                [],
            )

    @unittest.skipIf(
        os.name == "nt",
        "Creating symlinks is not reliably permitted on Windows CI.",
    )
    def test_rejects_source_symlink(self) -> None:
        link = self.root / "source-link.bin"
        link.symlink_to(self.source)
        spec = direct_destination_spec(
            vault_root=self.vault,
            digest=self.digest,
        )

        with self.assertRaisesRegex(
            IngestError,
            "symbolic link",
        ):
            ingest_object(
                source=link,
                destination_spec=spec,
            )

    def test_capsule_requires_canonical_archive_path(self) -> None:
        capsule = {
            "objects": [
                {
                    "id": "example",
                    "digest": self.digest,
                    "archive_path": "objects/not-canonical.bin",
                }
            ]
        }
        capsule_path = self.root / "capsule.json"
        capsule_path.write_text(
            json.dumps(capsule),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            IngestError,
            "not canonical",
        ):
            capsule_destination_spec(
                capsule_path=capsule_path,
                object_id="example",
                vault_root=self.vault,
            )


if __name__ == "__main__":
    unittest.main()
