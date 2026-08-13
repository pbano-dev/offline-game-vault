"""Unit tests for the Bottles host-contract migrator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.capsule_migrator import (
    MODERN_CONTRACT_NAME,
    MigrationError,
    NEW_CONTRACT_RELATIVE,
    migrate_bottles_contract,
)


LEGACY_FLATPAK_APP = "com.usebottles.bottles"


def _minimal_playable_wine_profile() -> dict:
    """A playable-wine profile the synthesiser can consume."""
    return {
        "id": "linux-direct-wine",
        "adapter": "wine",
        "platform": "linux",
        "dependencies": ["game-baseline", "ge-proton"],
        "launch": {
            "entrypoint": (
                "prefix/drive_c/Games/game/EP9/TSBin/game.exe"
            ),
            "working_directory": (
                "prefix/drive_c/Games/game/EP9/TSBin"
            ),
            "arguments": [],
            "network": "host_default",
        },
        "playable": {
            "schema": 0,
            "backend": "wine",
            "paths": {"prefix": "prefix"},
            "layout": [
                {
                    "object": "game-baseline",
                    "source": "neutral-object",
                    "destination": "prefix",
                },
            ],
        },
    }


def _minimal_legacy_bottles_profile() -> dict:
    return {
        "id": "linux-bottles-flatpak",
        "adapter": "bottles",
        "platform": "linux",
        "dependencies": ["game-baseline", "ge-proton"],
        "host_contract": "host-contracts/linux-bottles.json",
        "launch": {
            "entrypoint": (
                "drive_c/Games/game/EP9/TSBin/game.exe"
            ),
            "working_directory": (
                "drive_c/Games/game/EP9/TSBin"
            ),
            "arguments": [],
            "network": "isolated",
        },
    }


def _legacy_bottles_host_contract() -> dict:
    return {
        "schema": 0,
        "contract_id": "linux-x86_64-bottles-flatpak-test",
        "platform": "linux",
        "architecture": ["x86_64"],
        "capabilities": {
            "flatpak": {"level": "required"},
            "vulkan": {"level": "required"},
        },
        "graphics": {"tested_backend": "vulkan"},
        "notes": "Test fixture.",
    }


class MigrateBottlesContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.capsule_root = Path(self.tempdir.name) / "capsule"
        self.capsule_root.mkdir()
        (self.capsule_root / "host-contracts").mkdir()
        self.capsule_path = self.capsule_root / "capsule.json"
        self.legacy_hc_path = (
            self.capsule_root / "host-contracts/linux-bottles.json"
        )
        self._write_legacy_capsule()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # -------------------------------------- fixture builders

    def _write_legacy_capsule(self) -> None:
        capsule = {
            "schema": 0,
            "capsule_id": "steam-test-game-1.0",
            "game": {
                "title": "Test",
                "source_store": "Steam",
                "preserved_version": "1",
            },
            "objects": [],
            "persistent_state": [],
            "profiles": [
                _minimal_playable_wine_profile(),
                _minimal_legacy_bottles_profile(),
            ],
        }
        self.capsule_path.write_text(
            json.dumps(capsule, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.legacy_hc_path.write_text(
            json.dumps(_legacy_bottles_host_contract(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _reload_capsule(self) -> dict:
        return json.loads(self.capsule_path.read_text(encoding="utf-8"))

    def _bottles_profile(self, capsule: dict) -> dict:
        for p in capsule["profiles"]:
            if p.get("adapter") == "bottles":
                return p
        raise AssertionError("no bottles profile")

    # ---------------------------------------------- happy path

    def test_migrates_legacy_to_modern(self) -> None:
        report = migrate_bottles_contract(
            capsule_path=self.capsule_path,
            flatpak_app=LEGACY_FLATPAK_APP,
        )
        self.assertFalse(report["already_migrated"])
        self.assertEqual(report["new_contract_path"], NEW_CONTRACT_RELATIVE)
        self.assertEqual(report["legacy_contract_id"], "linux-x86_64-bottles-flatpak-test")

        # Legacy file is gone.
        self.assertFalse(self.legacy_hc_path.exists())

        # New file exists and declares the modern contract.
        new_hc_path = self.capsule_root / NEW_CONTRACT_RELATIVE
        self.assertTrue(new_hc_path.is_file())
        new_hc = json.loads(new_hc_path.read_text(encoding="utf-8"))
        self.assertEqual(new_hc["contract"], MODERN_CONTRACT_NAME)
        self.assertEqual(new_hc["flatpak_app"], LEGACY_FLATPAK_APP)
        # The 7 neutral fields are present.
        for field in (
            "source_object",
            "neutral_root",
            "prefix_source",
            "game_source",
            "game_destination_in_prefix",
            "entrypoint_relative_to_game",
            "working_directory_in_prefix",
        ):
            self.assertIn(field, new_hc)
        # derived_from carries the audit trail.
        self.assertEqual(
            new_hc["derived_from"]["legacy_contract_id"],
            "linux-x86_64-bottles-flatpak-test",
        )
        self.assertEqual(
            new_hc["derived_from"]["playable_profile_id"],
            "linux-direct-wine",
        )

        # capsule.json's bottles profile now points at the new file.
        capsule = self._reload_capsule()
        self.assertEqual(
            self._bottles_profile(capsule)["host_contract"],
            NEW_CONTRACT_RELATIVE,
        )

    # ---------------------------------------------- idempotency

    def test_already_migrated_returns_report_without_changes(self) -> None:
        # First migration.
        migrate_bottles_contract(
            capsule_path=self.capsule_path,
            flatpak_app=LEGACY_FLATPAK_APP,
        )
        new_hc_path = self.capsule_root / NEW_CONTRACT_RELATIVE
        mtime_before = new_hc_path.stat().st_mtime
        capsule_before = self.capsule_path.read_text(encoding="utf-8")

        # But the bottles profile now points at the new file, which
        # is already migrated. A second migration should detect that
        # and return early.
        report = migrate_bottles_contract(
            capsule_path=self.capsule_path,
            flatpak_app=LEGACY_FLATPAK_APP,
        )
        self.assertTrue(report["already_migrated"])
        # No file rewritten.
        self.assertEqual(new_hc_path.stat().st_mtime, mtime_before)
        self.assertEqual(
            self.capsule_path.read_text(encoding="utf-8"),
            capsule_before,
        )

    # ------------------------------------------ error branches

    def test_no_bottles_profile_errors(self) -> None:
        capsule = self._reload_capsule()
        capsule["profiles"] = [
            p for p in capsule["profiles"] if p.get("adapter") != "bottles"
        ]
        self.capsule_path.write_text(
            json.dumps(capsule, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError) as ctx:
            migrate_bottles_contract(
                capsule_path=self.capsule_path,
                flatpak_app=LEGACY_FLATPAK_APP,
            )
        self.assertIn("no Bottles profile", str(ctx.exception))

    def test_no_playable_wine_profile_errors(self) -> None:
        capsule = self._reload_capsule()
        # Remove the playable block from the wine profile.
        for p in capsule["profiles"]:
            if p.get("adapter") == "wine":
                p.pop("playable", None)
        self.capsule_path.write_text(
            json.dumps(capsule, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError) as ctx:
            migrate_bottles_contract(
                capsule_path=self.capsule_path,
                flatpak_app=LEGACY_FLATPAK_APP,
            )
        self.assertIn("playable-wine", str(ctx.exception))

    def test_missing_host_contract_file_errors(self) -> None:
        self.legacy_hc_path.unlink()
        with self.assertRaises(MigrationError) as ctx:
            migrate_bottles_contract(
                capsule_path=self.capsule_path,
                flatpak_app=LEGACY_FLATPAK_APP,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_not_a_legacy_contract_errors(self) -> None:
        """A host-contract without 'contract' AND without 'contract_id'
        is neither legacy nor modern; refuse rather than guess."""
        self.legacy_hc_path.write_text(
            json.dumps({"schema": 0, "some_other_shape": True}),
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError) as ctx:
            migrate_bottles_contract(
                capsule_path=self.capsule_path,
                flatpak_app=LEGACY_FLATPAK_APP,
            )
        self.assertIn("does not look like a legacy", str(ctx.exception))

    # --------------------------------------- dry-run and force

    def test_dry_run_writes_nothing(self) -> None:
        capsule_before = self.capsule_path.read_text(encoding="utf-8")
        legacy_before = self.legacy_hc_path.read_text(encoding="utf-8")
        report = migrate_bottles_contract(
            capsule_path=self.capsule_path,
            flatpak_app=LEGACY_FLATPAK_APP,
            dry_run=True,
        )
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["already_migrated"])
        # Legacy still there.
        self.assertTrue(self.legacy_hc_path.exists())
        # No new file.
        self.assertFalse(
            (self.capsule_root / NEW_CONTRACT_RELATIVE).exists()
        )
        # capsule.json untouched.
        self.assertEqual(
            self.capsule_path.read_text(encoding="utf-8"),
            capsule_before,
        )
        self.assertEqual(
            self.legacy_hc_path.read_text(encoding="utf-8"),
            legacy_before,
        )

    def test_existing_new_contract_without_force_errors(self) -> None:
        new_hc_path = self.capsule_root / NEW_CONTRACT_RELATIVE
        new_hc_path.parent.mkdir(parents=True, exist_ok=True)
        new_hc_path.write_text('{"contract":"pre-existing"}\n', encoding="utf-8")
        with self.assertRaises(MigrationError) as ctx:
            migrate_bottles_contract(
                capsule_path=self.capsule_path,
                flatpak_app=LEGACY_FLATPAK_APP,
            )
        self.assertIn("already exists", str(ctx.exception))
        # And nothing changed.
        self.assertTrue(self.legacy_hc_path.exists())

    def test_force_overwrites_existing_new_contract(self) -> None:
        new_hc_path = self.capsule_root / NEW_CONTRACT_RELATIVE
        new_hc_path.parent.mkdir(parents=True, exist_ok=True)
        new_hc_path.write_text('{"contract":"stale"}\n', encoding="utf-8")
        report = migrate_bottles_contract(
            capsule_path=self.capsule_path,
            flatpak_app=LEGACY_FLATPAK_APP,
            force=True,
        )
        self.assertFalse(report["already_migrated"])
        new_hc = json.loads(new_hc_path.read_text(encoding="utf-8"))
        self.assertEqual(new_hc["contract"], MODERN_CONTRACT_NAME)


if __name__ == "__main__":
    unittest.main()
