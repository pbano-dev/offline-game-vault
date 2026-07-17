from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.cli import main


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def archive_path(value: str) -> str:
    hexadecimal = value.removeprefix("sha256:")
    return (
        f"objects/sha256/{hexadecimal[:2]}/"
        f"{hexadecimal[2:4]}/{hexadecimal}"
    )


class ProfileAndInventoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source.tar.gz"
        self.payload = b"CLI profile object\n"
        self.source.write_bytes(self.payload)
        self.digest = digest(self.payload)

        self.capsule = self.root / "capsule.json"
        self.capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "cli-capsule",
                    "objects": [
                        {
                            "id": "object",
                            "digest": self.digest,
                            "format": "tar.gz",
                            "archive_path": archive_path(self.digest),
                        }
                    ],
                    "profiles": [
                        {
                            "id": "profile",
                            "dependencies": ["object"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ingest_verify_and_inventory(self) -> None:
        ingest_stdout = io.StringIO()
        with contextlib.redirect_stdout(ingest_stdout):
            ingest_code = main(
                [
                    "ingest-profile",
                    "--capsule",
                    str(self.capsule),
                    "--profile",
                    "profile",
                    "--vault-root",
                    str(self.vault),
                    "--source",
                    f"object={self.source}",
                    "--json",
                ]
            )
        self.assertEqual(ingest_code, 0)
        self.assertTrue(json.loads(ingest_stdout.getvalue())["complete"])

        verify_stdout = io.StringIO()
        with contextlib.redirect_stdout(verify_stdout):
            verify_code = main(
                [
                    "verify-profile",
                    "--capsule",
                    str(self.capsule),
                    "--profile",
                    "profile",
                    "--vault-root",
                    str(self.vault),
                    "--json",
                ]
            )
        self.assertEqual(verify_code, 0)
        self.assertTrue(json.loads(verify_stdout.getvalue())["verified"])

        inventory_stdout = io.StringIO()
        with contextlib.redirect_stdout(inventory_stdout):
            inventory_code = main(
                [
                    "inventory",
                    "--vault-root",
                    str(self.vault),
                    "--json",
                ]
            )
        self.assertEqual(inventory_code, 0)
        self.assertEqual(
            json.loads(inventory_stdout.getvalue())["object_count"],
            1,
        )

    def test_verify_missing_returns_one(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "verify-profile",
                    "--capsule",
                    str(self.capsule),
                    "--profile",
                    "profile",
                    "--vault-root",
                    str(self.vault),
                    "--json",
                ]
            )

        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["verified"])


if __name__ == "__main__":
    unittest.main()
