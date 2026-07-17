from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.cli import main


class VerifyObjectCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "object.bin"
        self.path.write_bytes(b"verified object\n")
        self.digest = (
            "sha256:"
            + hashlib.sha256(self.path.read_bytes()).hexdigest()
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_json_success_returns_zero(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "verify-object",
                    "--path",
                    str(self.path),
                    "--digest",
                    self.digest,
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["verified"])

    def test_mismatch_returns_one(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "verify-object",
                    "--path",
                    str(self.path),
                    "--digest",
                    "sha256:" + ("0" * 64),
                ]
            )
        self.assertEqual(code, 1)

    def test_invalid_arguments_return_two(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["verify-object", "--path", str(self.path)])

        self.assertEqual(code, 2)
        self.assertIn("Direct mode requires", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
