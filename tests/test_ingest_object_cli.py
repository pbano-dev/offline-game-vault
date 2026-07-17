from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.cli import main


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


if __name__ == "__main__":
    unittest.main()
