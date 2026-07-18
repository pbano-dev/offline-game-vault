from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import uuid

from jsonschema import Draft202012Validator, FormatChecker

from offline_game_vault.bottles_adapter import (
    BottlesAdapterError,
    DEPLOYMENT_RECEIPT_NAME,
    build_bottles_launch_plan,
    deploy_bottles_profile,
    remove_bottles_deployment,
    run_bottles_deployment,
    verify_bottles_deployment,
)


class BottlesAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.materialization = self.root / "materialization"
        self.bottles = self.root / "managed-bottles"
        self.fixture = self.root / "fixture"
        self.bottles.mkdir()
        self.fixture.mkdir()

        self.object_root = (
            self.materialization
            / "objects"
            / "bottle-object"
        )
        self.source_bottle = self.object_root / "Source-Bottle"
        self.game = (
            self.source_bottle
            / "drive_c"
            / "Games"
            / "TEST"
            / "game.exe"
        )
        self.game.parent.mkdir(parents=True)
        self.game.write_bytes(b"game-binary")
        save = (
            self.source_bottle
            / "drive_c"
            / "users"
            / "steamuser"
            / "Documents"
            / "save.dat"
        )
        save.parent.mkdir(parents=True)
        save.write_bytes(b"save")
        (self.source_bottle / "bottle.yml").write_text(
            "\n".join(
                [
                    "Arch: win64",
                    "Custom_Path: false",
                    "Name: Source-Bottle",
                    "Path: Source-Bottle",
                    "Runner: ge-proton11-1",
                    "Windows: win10",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        self.materialization.mkdir(exist_ok=True)
        self.materialization_receipt_id = str(uuid.uuid4())
        (
            self.materialization
            / "materialization-receipt.json"
        ).write_text(
            json.dumps(
                {
                    "schema": 0,
                    "receipt_id": self.materialization_receipt_id,
                    "capsule_id": "adapter-test",
                    "profile_id": "linux-bottles",
                    "destination": ".",
                    "objects": [
                        {
                            "id": "bottle-object",
                            "digest": "sha256:" + "1" * 64,
                            "destination": "objects/bottle-object",
                            "strategy": "extract",
                            "verified": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        self.capsule = {
            "schema": 0,
            "capsule_id": "adapter-test",
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
                    "id": "bottle-object",
                    "digest": "sha256:" + "1" * 64,
                    "roles": [
                        "game_payload",
                        "prefix_baseline",
                    ],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": (
                        "objects/sha256/11/11/" + "1" * 64
                    ),
                },
                {
                    "id": "runner-object",
                    "digest": "sha256:" + "2" * 64,
                    "roles": ["runner"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": (
                        "objects/sha256/22/22/" + "2" * 64
                    ),
                },
            ],
            "persistent_state": [
                {
                    "id": "save",
                    "path": (
                        "drive_c/users/steamuser/"
                        "Documents/save.dat"
                    ),
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                }
            ],
            "profiles": [
                {
                    "id": "linux-bottles",
                    "platform": "linux",
                    "adapter": "bottles",
                    "status": "candidate",
                    "dependencies": [
                        "bottle-object",
                        "runner-object",
                    ],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": (
                            "drive_c/Games/TEST/game.exe"
                        ),
                        "working_directory": (
                            "drive_c/Games/TEST"
                        ),
                        "arguments": ["--test"],
                        "network": "isolated",
                    },
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self._write_capsule()
        self.name = "Test-OGV-Restore"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_capsule(self) -> None:
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def _deploy(self):
        return deploy_bottles_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-bottles",
            materialization=self.materialization,
            bottles_path=self.bottles,
            bottle_name=self.name,
        )

    def test_deploys_mutable_derivative_without_changing_source(self) -> None:
        source_before = {
            path.relative_to(self.source_bottle).as_posix():
            path.read_bytes()
            for path in self.source_bottle.rglob("*")
            if path.is_file()
        }

        result = self._deploy()

        self.assertTrue(result.complete)
        self.assertEqual(result.bottle_name, self.name)
        self.assertEqual(result.runner, "ge-proton11-1")
        self.assertEqual(result.network, "isolated")

        target = self.bottles / self.name
        self.assertTrue(
            (
                target
                / "drive_c"
                / "Games"
                / "TEST"
                / "game.exe"
            ).is_file()
        )

        bottle_yml = (target / "bottle.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'Name: "{self.name}"', bottle_yml)
        self.assertIn(f'Path: "{self.name}"', bottle_yml)
        self.assertIn("Custom_Path: false", bottle_yml)
        self.assertIn("Runner: ge-proton11-1", bottle_yml)

        source_after = {
            path.relative_to(self.source_bottle).as_posix():
            path.read_bytes()
            for path in self.source_bottle.rglob("*")
            if path.is_file()
        }
        self.assertEqual(source_before, source_after)

        receipt_path = target / DEPLOYMENT_RECEIPT_NAME
        receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["destination"], ".")
        self.assertEqual(
            receipt["materialization_receipt_id"],
            self.materialization_receipt_id,
        )
        self.assertNotIn(str(self.root), receipt_text)

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "bottles-deployment-receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(receipt)
        )
        self.assertEqual(errors, [])

        self.assertEqual(
            list(self.bottles.glob(".ogv-stage-bottles-*")),
            [],
        )
        self.assertEqual(
            list(self.bottles.glob(".ogv-lock-bottles-*")),
            [],
        )

    def test_existing_destination_is_rejected(self) -> None:
        target = self.bottles / self.name
        target.mkdir()
        marker = target / "keep"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "already exists",
        ):
            self._deploy()

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_missing_entrypoint_is_rejected(self) -> None:
        self.game.unlink()

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "entrypoint",
        ):
            self._deploy()

        self.assertFalse((self.bottles / self.name).exists())

    def test_unsafe_symlink_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_text("outside", encoding="utf-8")
        os.symlink(
            outside,
            self.source_bottle / "escape-link",
        )

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "Unsafe symbolic link",
        ):
            self._deploy()

        self.assertFalse((self.bottles / self.name).exists())

    def test_invalid_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            BottlesAdapterError,
            "Bottle name must match",
        ):
            deploy_bottles_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-bottles",
                materialization=self.materialization,
                bottles_path=self.bottles,
                bottle_name="../escape",
            )

    def test_verify_and_build_sanitized_isolated_launch_plan(self) -> None:
        result = self._deploy()

        verification = verify_bottles_deployment(
            bottles_path=self.bottles,
            bottle_name=self.name,
        )
        self.assertTrue(verification.verified)
        self.assertEqual(
            verification.deployment_id,
            result.deployment_id,
        )

        plan, command = build_bottles_launch_plan(
            bottles_path=self.bottles,
            bottle_name=self.name,
        )
        self.assertEqual(plan.network, "isolated")
        self.assertIn("--unshare=network", plan.command)
        self.assertIn("<BOTTLES_PATH>", " ".join(plan.command))
        self.assertNotIn(str(self.root), " ".join(plan.command))
        self.assertIn(str(self.bottles / self.name), " ".join(command))
        self.assertEqual(command[-2:], ("--", "--test"))

    @mock.patch(
        "offline_game_vault.bottles_adapter.subprocess.run"
    )
    def test_run_uses_exact_isolated_command(self, run_mock) -> None:
        self._deploy()
        run_mock.return_value.returncode = 0

        plan, returncode = run_bottles_deployment(
            bottles_path=self.bottles,
            bottle_name=self.name,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(plan.network, "isolated")
        command = run_mock.call_args.args[0]
        self.assertIn("--unshare=network", command)
        self.assertIn("--command=bottles-cli", command)
        self.assertIn(self.name, command)

    def test_removal_requires_state_and_stopped_confirmations(self) -> None:
        result = self._deploy()

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "processes.*stopped",
        ):
            remove_bottles_deployment(
                bottles_path=self.bottles,
                bottle_name=self.name,
                confirm_state_preserved=True,
                confirm_stopped=False,
            )

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "Persistent state",
        ):
            remove_bottles_deployment(
                bottles_path=self.bottles,
                bottle_name=self.name,
                confirm_state_preserved=False,
                confirm_stopped=True,
            )

        removal = remove_bottles_deployment(
            bottles_path=self.bottles,
            bottle_name=self.name,
            confirm_state_preserved=True,
            confirm_stopped=True,
        )

        self.assertEqual(
            removal.deployment_id,
            result.deployment_id,
        )
        self.assertTrue(removal.removed)
        self.assertFalse((self.bottles / self.name).exists())
        self.assertEqual(
            list(self.bottles.glob(".ogv-remove-bottles-*")),
            [],
        )
        self.assertEqual(
            list(self.bottles.glob(".ogv-lock-bottles-*")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
