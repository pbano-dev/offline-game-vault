from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.verifier import (
    VerifyError,
    direct_object_spec,
    resolve_capsule_object,
    verify_object,
)


class VerifyObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.payload = b"offline-game-vault object\n"
        self.object_path = self.root / "object.bin"
        self.object_path.write_bytes(self.payload)
        self.digest = (
            "sha256:" + hashlib.sha256(self.payload).hexdigest()
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_matching_digest_passes(self) -> None:
        result = verify_object(
            direct_object_spec(
                path=self.object_path,
                digest=self.digest,
                expected_size=len(self.payload),
            )
        )
        self.assertTrue(result.verified)
        self.assertTrue(result.digest_match)
        self.assertTrue(result.size_match)

    def test_mismatching_digest_is_not_verified(self) -> None:
        result = verify_object(
            direct_object_spec(
                path=self.object_path,
                digest="sha256:" + ("0" * 64),
            )
        )
        self.assertFalse(result.verified)
        self.assertFalse(result.digest_match)

    def test_mismatching_size_is_not_verified(self) -> None:
        result = verify_object(
            direct_object_spec(
                path=self.object_path,
                digest=self.digest,
                expected_size=len(self.payload) + 1,
            )
        )
        self.assertFalse(result.verified)
        self.assertTrue(result.digest_match)
        self.assertFalse(result.size_match)

    def test_rejects_malformed_digest(self) -> None:
        with self.assertRaisesRegex(
            VerifyError,
            "Digest must use lowercase",
        ):
            direct_object_spec(
                path=self.object_path,
                digest="SHA256:1234",
            )

    def test_rejects_directory(self) -> None:
        with self.assertRaisesRegex(VerifyError, "regular file"):
            verify_object(
                direct_object_spec(
                    path=self.root,
                    digest=self.digest,
                )
            )

    @unittest.skipIf(
        os.name == "nt",
        "Creating symlinks is not reliably permitted on Windows CI.",
    )
    def test_rejects_symlink(self) -> None:
        link = self.root / "object-link.bin"
        link.symlink_to(self.object_path)

        with self.assertRaisesRegex(
            VerifyError,
            "symbolic link",
        ):
            verify_object(
                direct_object_spec(
                    path=link,
                    digest=self.digest,
                )
            )

    @unittest.skipIf(
        os.name == "nt",
        "Creating symlinks is not reliably permitted on Windows CI.",
    )
    def test_rejects_symlink_component_in_vault(self) -> None:
        external = self.root / "external"
        external.mkdir()
        (external / "object").write_bytes(self.payload)

        vault = self.root / "vault"
        vault.mkdir()
        (vault / "objects").symlink_to(
            external,
            target_is_directory=True,
        )

        capsule_path = self.root / "capsule.json"
        capsule_path.write_text(
            json.dumps(
                {
                    "objects": [
                        {
                            "id": "example",
                            "digest": self.digest,
                            "archive_path": "objects/object",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        spec = resolve_capsule_object(
            capsule_path=capsule_path,
            object_id="example",
            vault_root=vault,
        )
        with self.assertRaisesRegex(
            VerifyError,
            "contains a symbolic link",
        ):
            verify_object(spec)

    def test_resolves_capsule_object(self) -> None:
        vault = self.root / "vault"
        stored = vault / "objects/sha256/aa/object"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(self.payload)

        capsule = {
            "objects": [
                {
                    "id": "example",
                    "digest": self.digest,
                    "size": len(self.payload),
                    "archive_path": "objects/sha256/aa/object",
                }
            ]
        }
        capsule_path = self.root / "capsule.json"
        capsule_path.write_text(
            json.dumps(capsule),
            encoding="utf-8",
        )

        spec = resolve_capsule_object(
            capsule_path=capsule_path,
            object_id="example",
            vault_root=vault,
        )
        result = verify_object(spec)

        self.assertEqual(spec.object_id, "example")
        self.assertEqual(spec.path, stored.absolute())
        self.assertTrue(result.verified)

    def test_unknown_capsule_object_fails(self) -> None:
        capsule_path = self.root / "capsule.json"
        capsule_path.write_text(
            json.dumps({"objects": []}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            VerifyError,
            "Unknown object ID",
        ):
            resolve_capsule_object(
                capsule_path=capsule_path,
                object_id="missing",
                vault_root=self.root / "vault",
            )


if __name__ == "__main__":
    unittest.main()
