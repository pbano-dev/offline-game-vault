from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from offline_game_vault.cli import build_parser, main


class ExperimentalCliTests(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        parser = build_parser()

        runners = parser.parse_args(
            [
                "list-preserved-runners",
                "--collection-root",
                "vault",
                "--json",
            ]
        )
        self.assertEqual(runners.command, "list-preserved-runners")
        self.assertEqual(runners.collection_root, Path("vault"))

        templates = parser.parse_args(
            [
                "list-umu-templates",
                "--collection-root",
                "vault",
            ]
        )
        self.assertEqual(templates.command, "list-umu-templates")

        materialize = parser.parse_args(
            [
                "materialize-experimental",
                "--collection-root",
                "vault",
                "--capsule",
                "capsule.json",
                "--backend",
                "direct-wine",
                "--runner",
                "runner",
                "--destination",
                "output",
                "--play",
                "--",
                "-windowed",
            ]
        )
        self.assertEqual(
            materialize.command,
            "materialize-experimental",
        )
        self.assertEqual(materialize.backend, "direct-wine")
        self.assertTrue(materialize.play)
        self.assertEqual(materialize.arguments, ["--", "-windowed"])

    def test_missing_collection_is_reported_before_materialization(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "list-preserved-runners",
                    "--collection-root",
                    "missing-vault",
                ]
            )

        self.assertEqual(returncode, 2)
        self.assertIn("collection is not a regular directory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
