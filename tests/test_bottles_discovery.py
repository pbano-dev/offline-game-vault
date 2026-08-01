from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_game_vault.bottles_adapter import (
    BottlesAdapterError,
    _flatpak_bottles_cli_command,
    assert_bottle_registered,
    discover_bottles_path,
    require_bottles_managed_path,
)
from offline_game_vault.portable_bottles_runtime import (
    PortableBottlesError,
    _require_managed_root,
)


class BottlesDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.managed = self.root / "managed"
        self.managed.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _completed(
        self,
        stdout: str,
        *,
        returncode: int = 0,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["flatpak"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_query_is_network_isolated(self) -> None:
        command = _flatpak_bottles_cli_command(
            "--json",
            "info",
            "bottles-path",
        )
        self.assertEqual(
            command[:4],
            [
                "flatpak",
                "run",
                "--unshare=network",
                "--command=bottles-cli",
            ],
        )

    def test_discovers_json_path_reported_by_bottles(self) -> None:
        payload = '{"bottles_path": "' + str(self.managed) + '"}\n'
        with patch(
            "offline_game_vault.bottles_adapter._run_bottles_cli",
            return_value=self._completed(payload),
        ):
            self.assertEqual(discover_bottles_path(), self.managed.resolve())

    def test_discovers_plain_path_after_json_retry(self) -> None:
        responses = [
            self._completed("", returncode=2, stderr="unsupported"),
            self._completed(str(self.managed) + "\n"),
        ]
        with patch(
            "offline_game_vault.bottles_adapter._run_bottles_cli",
            side_effect=responses,
        ):
            self.assertEqual(discover_bottles_path(), self.managed.resolve())

    def test_arbitrary_bottles_path_is_rejected(self) -> None:
        with patch(
            "offline_game_vault.bottles_adapter.discover_bottles_path",
            return_value=self.managed.resolve(),
        ):
            with self.assertRaisesRegex(
                BottlesAdapterError,
                "does not match",
            ):
                require_bottles_managed_path(self.other)

    def test_registration_requires_exact_enumerated_name(self) -> None:
        payload = '{"bottles": ["Known", "Wanted"]}\n'
        with patch(
            "offline_game_vault.bottles_adapter._run_bottles_cli",
            return_value=self._completed(payload),
        ):
            assert_bottle_registered("Wanted")

        with patch(
            "offline_game_vault.bottles_adapter._run_bottles_cli",
            return_value=self._completed('{"bottles": ["Known"]}\n'),
        ):
            with self.assertRaisesRegex(
                BottlesAdapterError,
                "did not recognize",
            ):
                assert_bottle_registered("Wanted")

    def test_portable_runtime_rejects_bottle_outside_active_path(self) -> None:
        active = self.managed / "Active"
        active.mkdir()
        outside = self.other / "Outside"
        outside.mkdir()
        with patch(
            "offline_game_vault.portable_bottles_runtime."
            "_discover_bottles_path",
            return_value=self.managed.resolve(),
        ):
            self.assertEqual(
                _require_managed_root(active),
                active.resolve(),
            )
            with self.assertRaisesRegex(
                PortableBottlesError,
                "active Bottles managed directory",
            ):
                _require_managed_root(outside)


if __name__ == "__main__":
    unittest.main()
