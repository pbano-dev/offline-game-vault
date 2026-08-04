from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from test_bottles_adapter import BottlesAdapterTests
from test_umu_adapter import UmuAdapterTests

from offline_game_vault.bottles_adapter import (
    DEPLOYMENT_RECEIPT_NAME,
    deploy_bottles_profile,
    verify_bottles_deployment,
)
from offline_game_vault.state_manager import preserve_state
from offline_game_vault.umu_adapter import (
    RECEIPT_NAME,
    UmuAdapterError,
    materialize_umu_profile,
    verify_umu_materialization,
)


def _read_json(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"JSON root is not an object: {path.name}")
    return document


def _write_support_files(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        path = root / name
        path.write_text(
            f"synthetic support file: {name}\n",
            encoding="utf-8",
        )


def _assert_state_evidence(
    test: unittest.TestCase,
    *,
    root: Path,
    receipt: dict,
    backup_id: str,
    state_root: str,
) -> None:
    evidence = receipt.get("state_restore")
    test.assertIsInstance(evidence, dict)
    assert isinstance(evidence, dict)

    test.assertEqual(evidence.get("schema"), 0)
    test.assertEqual(evidence.get("backup_id"), backup_id)
    test.assertEqual(evidence.get("item_count"), 1)
    test.assertEqual(evidence.get("restored_count"), 1)
    test.assertEqual(evidence.get("missing_count"), 0)
    test.assertEqual(evidence.get("state_root"), state_root)
    test.assertIs(evidence.get("complete"), True)

    baseline_relative = evidence.get("baseline_receipt")
    restore_relative = evidence.get("restore_receipt")

    test.assertIsInstance(baseline_relative, str)
    test.assertIsInstance(restore_relative, str)
    assert isinstance(baseline_relative, str)
    assert isinstance(restore_relative, str)

    for value in (baseline_relative, restore_relative):
        relative = Path(value)
        test.assertFalse(relative.is_absolute())
        test.assertNotIn("..", relative.parts)

    baseline_path = root / baseline_relative
    restore_path = root / restore_relative

    test.assertTrue(baseline_path.is_file())
    test.assertFalse(baseline_path.is_symlink())
    test.assertTrue(restore_path.is_file())
    test.assertFalse(restore_path.is_symlink())

    baseline_digest = (
        "sha256:"
        + hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    )
    test.assertEqual(
        evidence.get("baseline_receipt_sha256"),
        baseline_digest,
    )

    baseline = _read_json(baseline_path)
    restore = _read_json(restore_path)

    test.assertEqual(baseline.get("backup_id"), backup_id)
    test.assertEqual(restore.get("backup_id"), backup_id)
    test.assertEqual(restore.get("status"), "completed")
    test.assertIs(restore.get("complete"), True)


class BottlesGenericStateRestorationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = BottlesAdapterTests(
            "test_deploys_mutable_derivative_without_changing_source"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

    def test_restores_verified_backup_and_records_evidence(self) -> None:
        fixture = self.fixture

        _write_support_files(
            fixture.fixture,
            (
                "README.md",
                "GAME.md",
                "CREDITS.md",
                "PRESERVED.md",
                "host-contract.json",
            ),
        )

        state_source = fixture.root / "generic-state-source"
        archived_save = (
            state_source
            / "drive_c"
            / "users"
            / "steamuser"
            / "Documents"
            / "save.dat"
        )
        archived_save.parent.mkdir(parents=True)
        archived_save.write_bytes(b"archived-save")

        backup = fixture.root / "generic-state-backup"
        captured = preserve_state(
            capsule_path=fixture.capsule_path,
            state_root=state_source,
            backup=backup,
            confirm_stopped=True,
        )

        deployed = deploy_bottles_profile(
            capsule_path=fixture.capsule_path,
            profile_id="linux-bottles",
            materialization=fixture.materialization,
            bottles_path=fixture.bottles,
            bottle_name=fixture.name,
            state_backup=backup,
            require_state_backup=True,
        )
        self.assertTrue(deployed.complete)

        target = fixture.bottles / fixture.name
        restored_save = (
            target
            / "drive_c"
            / "users"
            / "steamuser"
            / "Documents"
            / "save.dat"
        )

        self.assertEqual(restored_save.read_bytes(), b"archived-save")
        self.assertEqual(
            (
                fixture.source_bottle
                / "drive_c"
                / "users"
                / "steamuser"
                / "Documents"
                / "save.dat"
            ).read_bytes(),
            b"save",
        )

        receipt = _read_json(target / DEPLOYMENT_RECEIPT_NAME)
        _assert_state_evidence(
            self,
            root=target,
            receipt=receipt,
            backup_id=captured.backup_id,
            state_root=".",
        )

        verification = verify_bottles_deployment(
            bottles_path=fixture.bottles,
            bottle_name=fixture.name,
        )
        self.assertTrue(verification.verified)

        self.assertEqual(
            list(fixture.bottles.glob(".ogv-state-snapshot-*")),
            [],
        )
        self.assertEqual(
            list(fixture.bottles.glob(".ogv-stage-bottles-*")),
            [],
        )


class UmuGenericStateRestorationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = UmuAdapterTests(
            "test_materialize_run_verify_and_remove"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

    def _configure_generic_state(self) -> None:
        fixture = self.fixture

        _write_support_files(
            fixture.fixture,
            (
                "README.md",
                "GAME.md",
                "CREDITS.md",
                "PRESERVED.md",
                "host.json",
            ),
        )

        fixture.capsule["persistent_state"] = [
            {
                "id": "save",
                "path": "save.bin",
                "kind": "save",
                "backup": True,
                "sensitive": True,
                "required": True,
            }
        ]

        profile = fixture.capsule["profiles"][0]
        contract = profile["umu"]
        contract.pop("state_archives", None)
        contract["protected_manifests"] = [
            item
            for item in contract["protected_manifests"]
            if Path(item["source"]).name
            not in {"config.sha256", "save.sha256"}
        ]
        contract["paths"]["prefix"] = "payload/prefix"

        fixture.capsule_path.write_text(
            json.dumps(fixture.capsule),
            encoding="utf-8",
        )

    def _capture(self, name: str, content: bytes):
        fixture = self.fixture
        source = fixture.root / f"{name}-source"
        source.mkdir()
        (source / "save.bin").write_bytes(content)
        backup = fixture.root / f"{name}-backup"
        result = preserve_state(
            capsule_path=fixture.capsule_path,
            state_root=source,
            backup=backup,
            confirm_stopped=True,
        )
        return backup, result

    def test_restores_reuses_matching_baseline_and_rejects_other(self) -> None:
        fixture = self.fixture
        self._configure_generic_state()

        backup, captured = self._capture(
            "accepted",
            b"generic-accepted-save",
        )

        materialized = materialize_umu_profile(
            capsule_path=fixture.capsule_path,
            profile_id="linux-umu",
            vault_root=fixture.vault,
            destination=fixture.destination,
            state_backup=backup,
            require_state_backup=True,
        )
        self.assertTrue(materialized.complete)
        self.assertIsNone(materialized.selected_save)

        restored_save = (
            fixture.destination
            / "payload"
            / "prefix"
            / "save.bin"
        )
        self.assertEqual(
            restored_save.read_bytes(),
            b"generic-accepted-save",
        )

        receipt = _read_json(fixture.destination / RECEIPT_NAME)
        self.assertEqual(receipt.get("selected_save"), None)
        self.assertEqual(
            receipt.get("paths", {}).get("prefix"),
            "payload/prefix",
        )
        self.assertEqual(
            receipt.get("state_archives"),
            [],
        )

        _assert_state_evidence(
            self,
            root=fixture.destination,
            receipt=receipt,
            backup_id=captured.backup_id,
            state_root="payload/prefix",
        )

        verification = verify_umu_materialization(
            destination=fixture.destination
        )
        self.assertTrue(verification.verified)

        reused = materialize_umu_profile(
            capsule_path=fixture.capsule_path,
            profile_id="linux-umu",
            vault_root=fixture.vault,
            destination=fixture.destination,
            state_backup=backup,
            require_state_backup=True,
        )
        self.assertEqual(
            reused.receipt_id,
            materialized.receipt_id,
        )

        other_backup, _ = self._capture(
            "other",
            b"other-save",
        )
        with self.assertRaisesRegex(
            UmuAdapterError,
            "another persistent-state baseline",
        ):
            materialize_umu_profile(
                capsule_path=fixture.capsule_path,
                profile_id="linux-umu",
                vault_root=fixture.vault,
                destination=fixture.destination,
                state_backup=other_backup,
                require_state_backup=True,
            )

        self.assertEqual(
            restored_save.read_bytes(),
            b"generic-accepted-save",
        )
        self.assertEqual(
            list(
                fixture.destination.parent.glob(
                    ".ogv-umu-state-snapshot-*"
                )
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
