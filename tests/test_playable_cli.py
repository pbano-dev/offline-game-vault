from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.cli import build_parser, main


class PlayableCliTests(unittest.TestCase):
    def test_playable_commands_are_registered(self) -> None:
        parser = build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if action.dest == "command"
        )
        choices = set(subparsers.choices)
        self.assertTrue(
            {
                "materialize-playable",
                "verify-playable",
                "run-playable",
                "remove-playable",
            }.issubset(choices)
        )

    def test_verify_missing_playable_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "verify-playable",
                        "--destination",
                        str(missing),
                    ]
                )
        self.assertEqual(result, 2)
        self.assertIn("error", stderr.getvalue().casefold())


if __name__ == "__main__":
    unittest.main()
