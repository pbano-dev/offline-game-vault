"""Unit tests for the --no-state capability and the retirement of the
_neutral_fields_from_playable bridge.

These tests exercise:
  * The CLI parser accepts --no-state.
  * --no-state and --state-backup are mutually exclusive.
  * --no-state and --save-id are mutually exclusive.
  * _SOURCE_PRIORITIES["bottles"] no longer lists playable-wine.
  * composition no longer exposes _neutral_fields_from_playable.
  * capsule_migrator still owns the synthesis function locally.
  * _always_state_archive_ids enumerates only always-policy ids and
    tolerates malformed shapes.
"""

from __future__ import annotations

import argparse
import unittest

from offline_game_vault import cli, composition
from offline_game_vault.composition import (
    CompositionError,
    _SOURCE_PRIORITIES,
    _always_state_archive_ids,
)


class NoStateFlagCliTests(unittest.TestCase):
    """The parser and _command_compose gate --no-state correctly."""

    def _parse(self, extra_args: list[str]) -> argparse.Namespace:
        parser = cli.build_parser()
        base = [
            "compose",
            "--collection-root", "/tmp/vault",
            "--capsule", "/tmp/vault/02_CAPSULES/x/capsule.json",
            "--backend", "umu",
            "--runner", "some-runner-id",
            "--destination", "/tmp/dest",
        ]
        return parser.parse_args(base + extra_args)

    def test_no_state_flag_is_accepted(self) -> None:
        args = self._parse(["--no-state"])
        self.assertTrue(args.no_state)

    def test_no_state_defaults_false(self) -> None:
        args = self._parse([])
        self.assertFalse(args.no_state)

    def test_no_state_rejects_state_backup(self) -> None:
        args = self._parse(["--no-state", "--state-backup", "/tmp/b"])
        with self.assertRaises(CompositionError) as ctx:
            cli._command_compose(args)
        self.assertIn("mutually exclusive", str(ctx.exception))
        self.assertIn("--state-backup", str(ctx.exception))

    def test_no_state_rejects_save_id(self) -> None:
        args = self._parse(["--no-state", "--save-id", "slot1"])
        with self.assertRaises(CompositionError) as ctx:
            cli._command_compose(args)
        self.assertIn("mutually exclusive", str(ctx.exception))
        self.assertIn("--save-id", str(ctx.exception))


class SynthesizerRetirementTests(unittest.TestCase):
    """The runtime bridge is gone from composition; migrator still owns it."""

    def test_composition_no_longer_exposes_synthesizer(self) -> None:
        self.assertFalse(
            hasattr(composition, "_neutral_fields_from_playable"),
            "_neutral_fields_from_playable must be retired from composition",
        )

    def test_migrator_still_owns_synthesizer(self) -> None:
        from offline_game_vault import capsule_migrator
        self.assertTrue(
            hasattr(capsule_migrator, "_neutral_fields_from_playable"),
            "capsule_migrator must own _neutral_fields_from_playable locally",
        )

    def test_bottles_priorities_no_longer_include_playable_wine(self) -> None:
        self.assertNotIn(
            "playable-wine",
            _SOURCE_PRIORITIES["bottles"],
            "playable-wine must not be a bottles source; migrate first",
        )

    def test_playable_wine_still_valid_for_direct_wine_and_umu(self) -> None:
        # Guard: retirement is scoped to bottles. Wine and UMU keep
        # playable-wine as a fallback source (respectively priority 0
        # and 4).
        self.assertEqual(_SOURCE_PRIORITIES["direct-wine"]["playable-wine"], 0)
        self.assertEqual(_SOURCE_PRIORITIES["umu"]["playable-wine"], 4)


class AlwaysStateArchiveIdsTests(unittest.TestCase):
    """The helper that surfaces skipped ids under --no-state."""

    def test_empty_for_profile_without_umu_block(self) -> None:
        self.assertEqual(_always_state_archive_ids({}), [])

    def test_empty_for_profile_without_state_archives(self) -> None:
        self.assertEqual(_always_state_archive_ids({"umu": {}}), [])

    def test_lists_only_always_policy_ids(self) -> None:
        profile = {
            "umu": {
                "state_archives": [
                    {"id": "config", "policy": "always"},
                    {"id": "slot1", "policy": "selectable"},
                    {"id": "shaders", "policy": "always"},
                ],
            }
        }
        self.assertEqual(
            _always_state_archive_ids(profile),
            ["config", "shaders"],
        )

    def test_tolerates_malformed_entries(self) -> None:
        profile = {
            "umu": {
                "state_archives": [
                    "not-a-dict",
                    {"id": None, "policy": "always"},
                    {"id": "", "policy": "always"},
                    {"id": "ok", "policy": "always"},
                ],
            }
        }
        self.assertEqual(_always_state_archive_ids(profile), ["ok"])


if __name__ == "__main__":
    unittest.main()
