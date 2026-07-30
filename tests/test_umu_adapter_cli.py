from __future__ import annotations

import unittest

from offline_game_vault.cli import build_parser


class UmuCliTests(unittest.TestCase):
    def test_materialize_umu_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "materialize-umu",
                "--capsule",
                "capsule.json",
                "--profile",
                "linux-umu",
                "--vault-root",
                "vault",
                "--state-root",
                "state",
                "--destination",
                "playable",
                "--save",
                "main",
            ]
        )
        self.assertEqual(args.command, "materialize-umu")
        self.assertEqual(args.profile, "linux-umu")
        self.assertEqual(args.save, "main")

    def test_run_umu_parser_preserves_arguments(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-umu",
                "--destination",
                "playable",
                "--",
                "-flag",
            ]
        )
        self.assertEqual(args.command, "run-umu")
        self.assertEqual(args.arguments, ["--", "-flag"])

    def test_remove_umu_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "remove-umu",
                "--destination",
                "playable",
                "--confirm-state-preserved",
            ]
        )
        self.assertTrue(args.confirm_state_preserved)


if __name__ == "__main__":
    unittest.main()
