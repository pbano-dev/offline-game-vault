from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from offline_game_vault.catalog_view import (
    CatalogViewError,
    build_catalog,
    verify_catalog,
)
from offline_game_vault.cli import main


class CatalogViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        immutable = self.root / "01_IMMUTABLE_VAULT"
        payload = b"preserved object\n"
        hexadecimal = hashlib.sha256(payload).hexdigest()
        self.digest = "sha256:" + hexadecimal
        self.object_relative = (
            f"objects/sha256/{hexadecimal[:2]}/{hexadecimal[2:4]}/{hexadecimal}"
        )
        object_path = immutable / self.object_relative
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(payload)
        (immutable / "VAULT_INVENTORY.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "algorithm": "sha256",
                    "object_count": 1,
                    "total_bytes": len(payload),
                    "objects": [
                        {
                            "digest": self.digest,
                            "path": self.object_relative,
                            "bytes": len(payload),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        capsule = self.root / "02_CAPSULES/test-game/capsule.json"
        capsule.parent.mkdir(parents=True)
        capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "test-game",
                    "game": {"title": "Test Game"},
                    "objects": [
                        {
                            "id": "game-baseline",
                            "digest": self.digest,
                            "archive_path": self.object_relative,
                            "format": "tar.gz",
                            "size": len(payload),
                            "roles": ["game_payload", "prefix_baseline"],
                            "shared": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_builds_games_and_digest_references_idempotently(self) -> None:
        first = build_catalog(self.root)
        game_ref = (
            self.root / "00_CATALOG/GAMES/test-game/game-baseline.ogvref"
        )
        self.assertEqual(first["status"], "catalog-built")
        self.assertTrue(game_ref.is_file())
        reference = json.loads(game_ref.read_text(encoding="utf-8"))
        self.assertEqual(reference["digest"], self.digest)
        self.assertEqual(reference["kind"], "game")
        self.assertEqual(reference["consumers"][0]["title"], "Test Game")
        snapshot = {
            path.relative_to(self.root / "00_CATALOG").as_posix(): path.read_bytes()
            for path in (self.root / "00_CATALOG").rglob("*")
            if path.is_file()
        }

        build_catalog(self.root)

        rebuilt = {
            path.relative_to(self.root / "00_CATALOG").as_posix(): path.read_bytes()
            for path in (self.root / "00_CATALOG").rglob("*")
            if path.is_file()
        }
        self.assertEqual(rebuilt, snapshot)
        self.assertTrue(verify_catalog(self.root)["verified"])

    def test_verify_rejects_stale_catalog(self) -> None:
        build_catalog(self.root)
        target = self.root / "00_CATALOG/GAMES/test-game/game-baseline.ogvref"
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(CatalogViewError, "stale"):
            verify_catalog(self.root)

    def test_cli_build_and_verify(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                ["catalog-build", "--collection-root", str(self.root), "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["game_reference_count"], 1)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                ["catalog-verify", "--collection-root", str(self.root), "--json"]
            )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["verified"])


if __name__ == "__main__":
    unittest.main()
