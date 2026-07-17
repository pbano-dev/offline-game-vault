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
from offline_game_vault.storage import ingest_object
from offline_game_vault.verifier import ObjectSpec


class MaterializeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.destination = self.root / "materialized"
        self.archive = self.root / "object.tar.gz"

        payload_root = self.root / "payload"
        payload_root.mkdir()
        (payload_root / "file.txt").write_text(
            "materialized",
            encoding="utf-8",
        )
        with tarfile.open(self.archive, "w:gz") as archive:
            archive.add(
                payload_root / "file.txt",
                arcname="file.txt",
            )

        self.digest = (
            "sha256:"
            + hashlib.sha256(self.archive.read_bytes()).hexdigest()
        )
        hexadecimal = self.digest.removeprefix("sha256:")
        self.archive_path = (
            f"objects/sha256/{hexadecimal[:2]}/"
            f"{hexadecimal[2:4]}/{hexadecimal}"
        )

        self.capsule = self.fixture / "capsule.json"
        self.capsule.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "cli-materialize",
                    "game": {
                        "title": "Test",
                        "source_store": "Test",
                        "preserved_version": "1",
                    },
                    "documents": {
                        "readme": "README.md",
                        "game_sheet": "GAME.md",
                        "credits": "CREDITS.md",
                        "preserved_by": "PRESERVED.md",
                    },
                    "objects": [
                        {
                            "id": "object",
                            "digest": self.digest,
                            "roles": ["game_payload"],
                            "format": "tar.gz",
                            "required": True,
                            "archive_path": self.archive_path,
                        }
                    ],
                    "profiles": [
                        {
                            "id": "profile",
                            "platform": "linux",
                            "adapter": "bottles",
                            "status": "candidate",
                            "dependencies": ["object"],
                            "host_contract": "host-contract.json",
                            "launch": {
                                "entrypoint": "file.txt",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.fixture / "host-contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        destination = self.vault / self.archive_path
        ingest_object(
            source=self.archive,
            destination_spec=ObjectSpec(
                object_id="object",
                path=destination,
                expected_digest=self.digest,
                expected_size=None,
                vault_root=self.vault.resolve(),
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_materialize_and_remove_cli(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "materialize",
                    "--capsule",
                    str(self.capsule),
                    "--profile",
                    "profile",
                    "--vault-root",
                    str(self.vault),
                    "--destination",
                    str(self.destination),
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["complete"])
        self.assertTrue(self.destination.exists())

        remove_stdout = io.StringIO()
        with contextlib.redirect_stdout(remove_stdout):
            remove_code = main(
                [
                    "remove-materialization",
                    "--destination",
                    str(self.destination),
                    "--json",
                ]
            )

        self.assertEqual(remove_code, 0)
        removal = json.loads(remove_stdout.getvalue())
        self.assertTrue(removal["removed"])
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
