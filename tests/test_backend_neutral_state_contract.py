from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
import inspect
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from offline_game_vault.cli import _command_compose
from offline_game_vault.composition import (
    CompositionResult,
    compose_bottles,
    compose_umu,
)


class BackendNeutralStateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collection = Path("/synthetic/collection")
        self.capsule = Path("/synthetic/capsule.json")
        self.destination = Path("/synthetic/destination")
        self.state_backup = Path("/synthetic/state-backup")
        self.bottles_path = Path("/synthetic/bottles")

    @staticmethod
    def _result(backend: str) -> CompositionResult:
        return CompositionResult(
            schema=0,
            capsule_id="synthetic-game",
            backend=backend,
            runner_id="synthetic-runner",
            profile_id=f"composition-{backend}",
            destination="/synthetic/result",
            materialized=True,
            played=False,
            play_complete=None,
            backend_result={},
        )

    def _arguments(self, backend: str) -> Namespace:
        return Namespace(
            collection_root=self.collection,
            capsule=self.capsule,
            backend=backend,
            runner="synthetic-runner",
            source_profile=None,
            destination=self.destination,
            state_backup=self.state_backup,
            state_root=None,
            save_id=None,
            no_state=False,
            bottles_path=(
                self.bottles_path
                if backend == "bottles"
                else None
            ),
            bottle_name=(
                "SyntheticBottle"
                if backend == "bottles"
                else None
            ),
            play=False,
            json=True,
            arguments=(),
        )

    def test_bottles_compose_accepts_state_backup(self) -> None:
        self.assertIn(
            "state_backup",
            inspect.signature(compose_bottles).parameters,
        )

    def test_umu_compose_accepts_state_backup(self) -> None:
        self.assertIn(
            "state_backup",
            inspect.signature(compose_umu).parameters,
        )

    def test_cli_forwards_state_backup_to_bottles(self) -> None:
        with patch(
            "offline_game_vault.cli.compose_bottles",
            return_value=self._result("bottles"),
        ) as mocked:
            with redirect_stdout(io.StringIO()):
                returncode = _command_compose(
                    self._arguments("bottles")
                )

        self.assertEqual(returncode, 0)
        self.assertEqual(
            mocked.call_args.kwargs.get("state_backup"),
            self.state_backup,
        )
        self.assertEqual(
            mocked.call_args.kwargs.get("destination"),
            self.destination,
        )

    def test_cli_forwards_state_backup_to_umu(self) -> None:
        with patch(
            "offline_game_vault.cli.compose_umu",
            return_value=self._result("umu"),
        ) as mocked:
            with redirect_stdout(io.StringIO()):
                returncode = _command_compose(
                    self._arguments("umu")
                )

        self.assertEqual(returncode, 0)
        self.assertEqual(
            mocked.call_args.kwargs.get("state_backup"),
            self.state_backup,
        )


if __name__ == "__main__":
    unittest.main()
