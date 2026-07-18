from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
import uuid

from offline_game_vault.cli import main


class BottlesAdapterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.materialization = self.root / "materialization"
        self.bottles = self.root / "managed"
        self.fixture = self.root / "fixture"
        self.bottles.mkdir()
        self.fixture.mkdir()

        bottle = (
            self.materialization
            / "objects"
            / "bottle"
            / "Source"
        )
        executable = bottle / "drive_c" / "Game" / "game.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"game")
        (bottle / "bottle.yml").write_text(
            "\n".join(
                [
                    "Custom_Path: false",
                    "Name: Source",
                    "Path: Source",
                    "Runner: runner-1",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        (
            self.materialization
            / "materialization-receipt.json"
        ).write_text(
            json.dumps(
                {
                    "schema": 0,
                    "receipt_id": str(uuid.uuid4()),
                    "capsule_id": "cli-adapter",
                    "profile_id": "profile",
                    "destination": ".",
                    "objects": [
                        {
                            "id": "bottle",
                            "digest": "sha256:" + "a" * 64,
                            "destination": "objects/bottle",
                            "strategy": "extract",
                            "verified": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        self.capsule = self.fixture / "capsule.json"
        self.capsule.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "cli-adapter",
                    "game": {
                        "title": "Test",
                        "source_store": "Test",
                        "preserved_version": "1",
                    },
                    "documents": {
                        "readme": "README.md",
                        "game_sheet": "GAME.md",
                        "credits": "CREDITS.md",
                        "preserved_by": "PRESERVED.md",
                    },
                    "objects": [
                        {
                            "id": "bottle",
                            "digest": "sha256:" + "a" * 64,
                            "roles": ["prefix_baseline"],
                            "format": "tar.gz",
                            "required": True,
                            "archive_path": (
                                "objects/sha256/aa/aa/"
                                + "a" * 64
                            ),
                        }
                    ],
                    "profiles": [
                        {
                            "id": "profile",
                            "platform": "linux",
                            "adapter": "bottles",
                            "status": "candidate",
                            "dependencies": ["bottle"],
                            "host_contract": "host-contract.json",
                            "launch": {
                                "entrypoint": (
                                    "drive_c/Game/game.exe"
                                ),
                                "arguments": [],
                                "network": "isolated",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.name = "CLI-OGV-Restore"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_deploy_verify_plan_and_remove(self) -> None:
        deploy_stdout = io.StringIO()
        with contextlib.redirect_stdout(deploy_stdout):
            deploy_code = main(
                [
                    "deploy-bottles",
                    "--capsule",
                    str(self.capsule),
                    "--profile",
                    "profile",
                    "--materialization",
                    str(self.materialization),
                    "--bottles-path",
                    str(self.bottles),
                    "--name",
                    self.name,
                    "--json",
                ]
            )

        self.assertEqual(deploy_code, 0)
        deploy_result = json.loads(deploy_stdout.getvalue())
        self.assertTrue(deploy_result["complete"])
        self.assertNotIn(str(self.root), deploy_stdout.getvalue())

        verify_stdout = io.StringIO()
        with contextlib.redirect_stdout(verify_stdout):
            verify_code = main(
                [
                    "verify-bottles-deployment",
                    "--bottles-path",
                    str(self.bottles),
                    "--name",
                    self.name,
                    "--json",
                ]
            )

        self.assertEqual(verify_code, 0)
        self.assertTrue(json.loads(verify_stdout.getvalue())["verified"])

        plan_stdout = io.StringIO()
        with contextlib.redirect_stdout(plan_stdout):
            plan_code = main(
                [
                    "plan-bottles-launch",
                    "--bottles-path",
                    str(self.bottles),
                    "--name",
                    self.name,
                    "--json",
                ]
            )

        self.assertEqual(plan_code, 0)
        plan = json.loads(plan_stdout.getvalue())
        self.assertIn("--unshare=network", plan["command"])
        self.assertNotIn(str(self.root), plan_stdout.getvalue())

        remove_stdout = io.StringIO()
        with contextlib.redirect_stdout(remove_stdout):
            remove_code = main(
                [
                    "remove-bottles-deployment",
                    "--bottles-path",
                    str(self.bottles),
                    "--name",
                    self.name,
                    "--confirm-stopped",
                    "--json",
                ]
            )

        self.assertEqual(remove_code, 0)
        self.assertTrue(json.loads(remove_stdout.getvalue())["removed"])
        self.assertFalse((self.bottles / self.name).exists())


if __name__ == "__main__":
    unittest.main()
