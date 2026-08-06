from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from offline_game_vault import portable_external_bottles_runtime as runtime


class ExternalBottlesRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "external"
        self.managed = self.base / "bottles-data/bottles"
        self.runner = self.base / "bottles-data/runners/ge-proton"
        self.managed.mkdir(parents=True)
        self.runner.mkdir(parents=True)
        self.other = self.managed / "OtherBottle"
        self.other.mkdir()

        prefix = self.root / "payload/prefix"
        game = self.root / "payload/game"
        prefix.mkdir(parents=True)
        game.mkdir(parents=True)
        (game / "game.exe").write_bytes(b"synthetic")
        (prefix / "bottle.yml").write_text(
            "Name: \"ExampleBottle\"\n"
            "Path: \"ExampleBottle\"\n"
            "Custom_Path: false\n"
            "Runner: \"ge-proton\"\n",
            encoding="utf-8",
        )

        metadata = self.root / "metadata"
        metadata.mkdir()
        operational = {
            "launcher": "JUGAR.sh",
            "verifier": "VERIFICAR.sh",
            "uninstaller": "DESINSTALAR.sh",
            "portable_runtime":
                "metadata/ogv_external_bottles_runtime.py",
        }
        for relative in operational.values():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)

        receipt = {
            "schema": 0,
            "adapter": "bottles-flatpak",
            "destination": ".",
            "capsule_id": "example-game",
            "profile_id": "composition-bottles",
            "bottle_name": "ExampleBottle",
            "runner": "ge-proton",
            "layout": {
                "kind": "external-wrapper-v1",
                "prefix": "payload/prefix",
                "game": "payload/game",
                "registration_target": "payload/prefix",
                "game_destination_in_prefix": "drive_c/Games/example",
            },
            "launch": {
                "entrypoint": "payload/game/game.exe",
                "arguments": ["--configured"],
                "network": "isolated",
            },
            "operational_paths": operational,
            "persistent_state": [],
        }
        (self.root / runtime.RECEIPT_NAME).write_text(
            json.dumps(receipt),
            encoding="utf-8",
        )

    def patches(self):
        return (
            patch.object(runtime, "_flatpak_info"),
            patch.object(
                runtime,
                "_discover_bottles_path",
                return_value=self.managed.resolve(),
            ),
            patch.object(runtime, "_assert_enumerated"),
        )

    def test_bottles_64_json_mapping_is_enumerated(self) -> None:
        output = json.dumps(
            {
                "ExampleBottle": {
                    "Name": "ExampleBottle",
                    "Path": "ExampleBottle",
                }
            }
        )
        self.assertEqual(
            runtime._enumerated_names(output),
            {"ExampleBottle"},
        )

    def test_play_recreates_registration_and_uses_external_root(self) -> None:
        info, discover, enumerated = self.patches()
        with info, discover, enumerated:
            command, verification = runtime._play_command(
                self.root,
                flatpak_app=runtime.DEFAULT_FLATPAK_APP,
                arguments=("--extra",),
            )

        registration = self.managed / "ExampleBottle"
        self.assertTrue(registration.is_symlink())
        self.assertEqual(
            registration.resolve(),
            (self.root / "payload/prefix").resolve(),
        )
        canonical = self.root.resolve()
        self.assertEqual(
            command,
            [
                "flatpak",
                "run",
                f"--filesystem={canonical}",
                "--unshare=network",
                "--command=bottles-cli",
                runtime.DEFAULT_FLATPAK_APP,
                "run",
                "-b",
                "ExampleBottle",
                "-e",
                str(canonical / "payload/game/game.exe"),
                "--",
                "--configured",
                "--extra",
            ],
        )
        self.assertTrue(verification["verified"])

    def test_uninstall_removes_only_registration_and_external_root(self) -> None:
        registration = self.managed / "ExampleBottle"
        registration.symlink_to(
            self.root / "payload/prefix",
            target_is_directory=True,
        )
        info, discover, enumerated = self.patches()
        with info, discover, enumerated:
            result = runtime.uninstall(
                self.root,
                confirm_stopped=True,
                confirm_state_preserved=True,
            )

        self.assertTrue(result["removed"])
        self.assertFalse(self.root.exists())
        self.assertFalse(registration.exists())
        self.assertFalse(registration.is_symlink())
        self.assertTrue(self.other.is_dir())
        self.assertTrue(self.runner.is_dir())

    def test_uninstall_rejects_registration_for_another_target(self) -> None:
        foreign = self.base / "foreign-prefix"
        foreign.mkdir()
        registration = self.managed / "ExampleBottle"
        registration.symlink_to(foreign, target_is_directory=True)
        info, discover, enumerated = self.patches()
        with info, discover, enumerated:
            with self.assertRaisesRegex(
                runtime.PortableExternalBottlesError,
                "another materialization",
            ):
                runtime.uninstall(
                    self.root,
                    confirm_stopped=True,
                    confirm_state_preserved=True,
                )

        self.assertTrue(self.root.is_dir())
        self.assertTrue(registration.is_symlink())
        self.assertEqual(registration.resolve(), foreign.resolve())
        self.assertTrue(self.other.is_dir())
        self.assertTrue(self.runner.is_dir())


if __name__ == "__main__":
    unittest.main()
