from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.planner import PlanError, build_plan


DIGEST = "sha256:" + ("a" * 64)


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.fixture = self.root / "fixture"
        self.vault = self.root / "vault"
        self.destination = self.root / "materialized"
        self.fixture.mkdir()
        self.vault.mkdir()

        (self.fixture / "host-contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        self.capsule = {
            "schema": 0,
            "capsule_id": "example-game",
            "objects": [
                {
                    "id": "game-object",
                    "digest": DIGEST,
                    "roles": ["game_payload"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": "objects/game.tar.gz",
                    "shared": False,
                }
            ],
            "profiles": [
                {
                    "id": "linux-bottles",
                    "platform": "linux",
                    "adapter": "bottles",
                    "status": "candidate",
                    "dependencies": ["game-object"],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": "drive_c/Games/Game/game.exe",
                        "working_directory": "drive_c/Games/Game",
                        "network": "isolated",
                    },
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self._write_capsule()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_capsule(self) -> None:
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def _create_object(self) -> None:
        path = self.vault / "objects/game.tar.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test object")

    def test_builds_plan_without_mutating_vault(self) -> None:
        self._create_object()

        plan = build_plan(
            capsule_path=self.capsule_path,
            profile_id="linux-bottles",
            vault_root=self.vault,
            destination=self.destination,
        )

        self.assertFalse(plan.mutates_vault)
        self.assertEqual(plan.profile_id, "linux-bottles")
        self.assertEqual(plan.objects[0].strategy, "extract")
        self.assertTrue(plan.objects[0].present)
        self.assertFalse(self.destination.exists())

    def test_rejects_destination_inside_vault(self) -> None:
        self._create_object()

        with self.assertRaisesRegex(
            PlanError,
            "outside the immutable vault",
        ):
            build_plan(
                capsule_path=self.capsule_path,
                profile_id="linux-bottles",
                vault_root=self.vault,
                destination=self.vault / "working",
            )

    def test_missing_required_object_fails_by_default(self) -> None:
        with self.assertRaisesRegex(
            PlanError,
            "Missing required object",
        ):
            build_plan(
                capsule_path=self.capsule_path,
                profile_id="linux-bottles",
                vault_root=self.vault,
                destination=self.destination,
            )

    def test_allow_missing_keeps_missing_explicit(self) -> None:
        plan = build_plan(
            capsule_path=self.capsule_path,
            profile_id="linux-bottles",
            vault_root=self.vault,
            destination=self.destination,
            allow_missing=True,
        )

        self.assertEqual(
            plan.missing_required_objects,
            ("game-object",),
        )
        self.assertFalse(plan.objects[0].present)

    def test_unknown_profile_fails(self) -> None:
        self._create_object()

        with self.assertRaisesRegex(PlanError, "Unknown profile"):
            build_plan(
                capsule_path=self.capsule_path,
                profile_id="windows-native",
                vault_root=self.vault,
                destination=self.destination,
            )

    def test_unknown_dependency_fails(self) -> None:
        self.capsule["profiles"][0]["dependencies"] = [
            "missing-object"
        ]
        self._write_capsule()

        with self.assertRaisesRegex(
            PlanError,
            "unknown object",
        ):
            build_plan(
                capsule_path=self.capsule_path,
                profile_id="linux-bottles",
                vault_root=self.vault,
                destination=self.destination,
                allow_missing=True,
            )


if __name__ == "__main__":
    unittest.main()
