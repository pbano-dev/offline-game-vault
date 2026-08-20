from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from offline_game_vault import cli, composition


class FreshStartParserTests(unittest.TestCase):
    def _parse(self, extra: list[str]):
        return cli.build_parser().parse_args(
            [
                "compose",
                "--collection-root", "/synthetic/vault",
                "--capsule", "/synthetic/capsule.json",
                "--backend", "umu",
                "--runner", "runner",
                "--destination", "/synthetic/output",
                *extra,
            ]
        )

    def test_fresh_start_flag_is_accepted(self) -> None:
        self.assertTrue(self._parse(["--fresh-start"]).fresh_start)

    def test_fresh_start_defaults_false(self) -> None:
        self.assertFalse(self._parse([]).fresh_start)

    def test_fresh_start_rejects_no_state(self) -> None:
        args = self._parse(["--fresh-start", "--no-state"])
        with self.assertRaisesRegex(
            composition.CompositionError,
            "--fresh-start and --no-state",
        ):
            cli._command_compose(args)

    def test_fresh_start_rejects_state_backup(self) -> None:
        args = self._parse(
            ["--fresh-start", "--state-backup", "/synthetic/backup"]
        )
        with self.assertRaisesRegex(
            composition.CompositionError,
            "--fresh-start and --state-backup",
        ):
            cli._command_compose(args)

    def test_fresh_start_rejects_save_id(self) -> None:
        args = self._parse(["--fresh-start", "--save-id", "slot-a"])
        with self.assertRaisesRegex(
            composition.CompositionError,
            "--fresh-start and --save-id",
        ):
            cli._command_compose(args)


class FreshStartTranslationTests(unittest.TestCase):
    def test_native_umu_fresh_start_keeps_materializer_state_enabled(
        self,
    ) -> None:
        self.assertFalse(
            composition._effective_materializer_no_state(
                source_kind="umu-native",
                fresh_start=True,
                no_state=False,
            )
        )

    def test_generic_fresh_start_skips_restorable_generic_state(self) -> None:
        self.assertTrue(
            composition._effective_materializer_no_state(
                source_kind="playable-wine",
                fresh_start=True,
                no_state=False,
            )
        )

    def test_no_state_remains_stronger_for_native_umu(self) -> None:
        self.assertTrue(
            composition._effective_materializer_no_state(
                source_kind="umu-native",
                fresh_start=False,
                no_state=True,
            )
        )


class FreshStartCliForwardingTests(unittest.TestCase):
    @staticmethod
    def _result(backend: str) -> SimpleNamespace:
        return SimpleNamespace(
            capsule_id="capsule",
            backend=backend,
            runner_id="runner",
            profile_id="profile",
            destination="/synthetic/output",
            materialized=True,
            played=False,
            play_complete=None,
            to_dict=lambda: {"backend": backend},
        )

    def _run(
        self,
        *,
        backend: str,
        target: str,
        extra: list[str] | None = None,
    ) -> None:
        argv = [
            "compose",
            "--collection-root", "/synthetic/vault",
            "--capsule", "/synthetic/capsule.json",
            "--backend", backend,
            "--runner", "runner",
            "--destination", "/synthetic/output",
            "--fresh-start",
            "--json",
        ]
        if extra:
            argv.extend(extra)
        args = cli.build_parser().parse_args(argv)
        with patch(target, return_value=self._result(backend)) as mocked:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli._command_compose(args)
        self.assertEqual(rc, 0)
        self.assertTrue(mocked.call_args.kwargs["fresh_start"])
        self.assertFalse(mocked.call_args.kwargs["no_state"])
        self.assertIsNone(mocked.call_args.kwargs["state_backup"])

    def test_forwards_to_direct_wine(self) -> None:
        self._run(
            backend="direct-wine",
            target="offline_game_vault.cli.compose_wine",
        )

    def test_forwards_to_umu(self) -> None:
        self._run(
            backend="umu",
            target="offline_game_vault.cli.compose_umu",
        )

    def test_forwards_to_bottles(self) -> None:
        self._run(
            backend="bottles",
            target="offline_game_vault.cli.compose_bottles",
            extra=["--bottle-name", "fresh-start-test"],
        )


class FreshStartApiValidationTests(unittest.TestCase):
    def test_python_api_rejects_ambiguous_fresh_start(self) -> None:
        with self.assertRaisesRegex(
            composition.CompositionError,
            "fresh_start and no_state",
        ):
            composition._validate_fresh_start_intent(
                fresh_start=True,
                no_state=True,
                state_backup=None,
            )
        with self.assertRaisesRegex(
            composition.CompositionError,
            "fresh_start and state_backup",
        ):
            composition._validate_fresh_start_intent(
                fresh_start=True,
                no_state=False,
                state_backup=Path("/synthetic/backup"),
            )


if __name__ == "__main__":
    unittest.main()
