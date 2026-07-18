from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

import offline_game_vault.state_manager as state_manager
from offline_game_vault.state_manager import (
    BACKUP_RECEIPT_NAME,
    RESTORE_RECEIPT_NAME,
    StateError,
    audit_capsule,
    preserve_state,
    restore_state,
    verify_state_backup,
)


class StateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        self.state_root = self.root / "state"
        self.fixture.mkdir()
        self.state_root.mkdir()
        self.capsule_path = self.fixture / "capsule.json"
        self._write_support_files()
        self._write_capsule()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_support_files(self) -> None:
        for name in (
            "README.md",
            "GAME.md",
            "CREDITS.md",
            "PRESERVED.md",
        ):
            (self.fixture / name).write_text(
                f"{name}\n",
                encoding="utf-8",
            )
        (self.fixture / "host-contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

    def _capsule(
        self,
        *,
        state: list[dict[str, object]] | None = None,
        sanitized: bool = False,
    ) -> dict[str, object]:
        if state is None:
            state = [
                {
                    "id": "config",
                    "path": "config",
                    "kind": "configuration",
                    "backup": True,
                    "sensitive": False,
                    "required": True,
                },
                {
                    "id": "save",
                    "path": "save/data.bin",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                },
            ]

        capsule: dict[str, object] = {
            "schema": 0,
            "capsule_id": "test-state-capsule",
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
                    "id": "payload",
                    "digest": "sha256:" + "a" * 64,
                    "roles": ["game_payload"],
                    "format": "file",
                    "required": True,
                    "archive_path": (
                        "objects/sha256/aa/aa/" + "a" * 64
                    ),
                }
            ],
            "persistent_state": state,
            "profiles": [
                {
                    "id": "linux-wine",
                    "platform": "linux",
                    "adapter": "wine",
                    "status": "candidate",
                    "dependencies": ["payload"],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": "game.exe",
                        "network": "isolated",
                    },
                }
            ],
        }
        if sanitized:
            capsule["sanitized_fixture"] = True
        return capsule

    def _write_capsule(
        self,
        *,
        state: list[dict[str, object]] | None = None,
        sanitized: bool = False,
    ) -> None:
        self.capsule_path.write_text(
            json.dumps(
                self._capsule(
                    state=state,
                    sanitized=sanitized,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_live_state(
        self,
        *,
        save: bytes,
        config: str,
    ) -> None:
        save_path = self.state_root / "save" / "data.bin"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(save)

        config_root = self.state_root / "config"
        (config_root / "sub").mkdir(parents=True, exist_ok=True)
        (config_root / "settings.ini").write_text(
            config,
            encoding="utf-8",
        )
        (config_root / "sub" / "identity.txt").write_text(
            "identity\n",
            encoding="utf-8",
        )

    def _validate_schema(
        self,
        document: dict[str, object],
        schema_name: str,
    ) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / schema_name
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(document)
        )
        self.assertEqual(errors, [])

    def test_preserve_verify_and_restore_round_trip(self) -> None:
        self._write_live_state(save=b"original-save", config="old\n")
        backup = self.root / "backup"

        result = preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_root,
            backup=backup,
            confirm_stopped=True,
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.present_count, 2)
        self.assertEqual(
            stat.S_IMODE(backup.stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                (backup / BACKUP_RECEIPT_NAME).stat().st_mode
            ),
            0o600,
        )

        verification = verify_state_backup(
            capsule_path=self.capsule_path,
            backup=backup,
        )
        self.assertTrue(verification.verified)

        backup_document = json.loads(
            (backup / BACKUP_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        self._validate_schema(
            backup_document,
            "state-backup.schema.json",
        )
        self.assertNotIn(str(self.root), json.dumps(backup_document))

        self._write_live_state(save=b"new-save", config="new\n")
        snapshot = self.root / "snapshot"
        restored = restore_state(
            capsule_path=self.capsule_path,
            state_root=self.state_root,
            backup=backup,
            snapshot=snapshot,
            confirm_stopped=True,
        )

        self.assertTrue(restored.complete)
        self.assertEqual(
            (self.state_root / "save" / "data.bin").read_bytes(),
            b"original-save",
        )
        self.assertEqual(
            (
                self.state_root / "config" / "settings.ini"
            ).read_text(encoding="utf-8"),
            "old\n",
        )

        restore_document = json.loads(
            (snapshot / RESTORE_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        self._validate_schema(
            restore_document,
            "state-restore-receipt.schema.json",
        )
        self.assertEqual(restore_document["status"], "completed")
        self.assertNotIn(str(self.root), json.dumps(restore_document))

        snapshot_verification = verify_state_backup(
            capsule_path=self.capsule_path,
            backup=snapshot,
        )
        self.assertTrue(snapshot_verification.verified)

    def test_required_state_must_exist(self) -> None:
        with self.assertRaisesRegex(
            StateError,
            "Required persistent state is missing",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=self.root / "backup",
                confirm_stopped=True,
            )

    def test_optional_missing_state_is_recorded(self) -> None:
        self._write_capsule(
            state=[
                {
                    "id": "optional-save",
                    "path": "save/optional.bin",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": False,
                }
            ]
        )
        backup = self.root / "backup"

        result = preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_root,
            backup=backup,
            confirm_stopped=True,
        )

        self.assertEqual(result.missing_count, 1)
        self.assertEqual(result.present_count, 0)
        verification = verify_state_backup(
            capsule_path=self.capsule_path,
            backup=backup,
        )
        self.assertTrue(verification.verified)

    def test_symlink_in_state_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        (self.state_root / "save").mkdir()
        os.symlink(
            outside,
            self.state_root / "save" / "data.bin",
        )
        (self.state_root / "config").mkdir()

        with self.assertRaisesRegex(
            StateError,
            "symbolic link",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=self.root / "backup",
                confirm_stopped=True,
            )

    def test_hardlinked_state_file_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"shared")
        (self.state_root / "save").mkdir()
        os.link(
            outside,
            self.state_root / "save" / "data.bin",
        )
        (self.state_root / "config").mkdir()

        with self.assertRaisesRegex(
            StateError,
            "multiple hard links",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=self.root / "backup",
                confirm_stopped=True,
            )

    def test_tampered_payload_does_not_verify(self) -> None:
        self._write_live_state(save=b"save", config="config\n")
        backup = self.root / "backup"
        preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_root,
            backup=backup,
            confirm_stopped=True,
        )
        document = json.loads(
            (backup / BACKUP_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        save_item = next(
            item for item in document["items"]
            if item["id"] == "save"
        )
        payload = backup / save_item["payload_path"]
        payload.write_bytes(b"tampered")
        os.chmod(payload, 0o600)

        verification = verify_state_backup(
            capsule_path=self.capsule_path,
            backup=backup,
        )

        self.assertFalse(verification.verified)
        self.assertTrue(verification.problems)
        self.assertNotIn(
            str(self.root),
            "\n".join(verification.problems),
        )

    def test_restore_failure_rolls_back_live_state(self) -> None:
        self._write_live_state(save=b"archive", config="archive\n")
        backup = self.root / "backup"
        preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_root,
            backup=backup,
            confirm_stopped=True,
        )

        self._write_live_state(save=b"live", config="live\n")
        original_apply = state_manager._apply_backup_item
        backup_root = backup.resolve()

        def fail_on_save(**kwargs):
            if (
                kwargs["backup_root"] == backup_root
                and kwargs["declaration"].id == "save"
            ):
                raise StateError("injected restore failure")
            return original_apply(**kwargs)

        snapshot = self.root / "snapshot"
        with mock.patch.object(
            state_manager,
            "_apply_backup_item",
            side_effect=fail_on_save,
        ):
            with self.assertRaisesRegex(
                StateError,
                "rollback.*completed",
            ):
                restore_state(
                    capsule_path=self.capsule_path,
                    state_root=self.state_root,
                    backup=backup,
                    snapshot=snapshot,
                    confirm_stopped=True,
                )

        self.assertEqual(
            (self.state_root / "save" / "data.bin").read_bytes(),
            b"live",
        )
        self.assertEqual(
            (
                self.state_root / "config" / "settings.ini"
            ).read_text(encoding="utf-8"),
            "live\n",
        )
        receipt = json.loads(
            (snapshot / RESTORE_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertTrue(receipt["rollback_complete"])
        self._validate_schema(
            receipt,
            "state-restore-receipt.schema.json",
        )

    def test_stopped_confirmation_is_mandatory(self) -> None:
        self._write_live_state(save=b"save", config="config\n")
        with self.assertRaisesRegex(
            StateError,
            "confirm-stopped",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=self.root / "backup",
                confirm_stopped=False,
            )

    def test_backup_must_not_overlap_state_root(self) -> None:
        self._write_live_state(save=b"save", config="config\n")

        with self.assertRaisesRegex(
            StateError,
            "must not overlap",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=self.state_root / "private-backup",
                confirm_stopped=True,
            )

    def test_backup_destination_is_never_overwritten(self) -> None:
        self._write_live_state(save=b"save", config="config\n")
        backup = self.root / "backup"
        backup.mkdir()
        marker = backup / "keep"
        marker.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(
            StateError,
            "already exists",
        ):
            preserve_state(
                capsule_path=self.capsule_path,
                state_root=self.state_root,
                backup=backup,
                confirm_stopped=True,
            )

        self.assertEqual(
            marker.read_text(encoding="utf-8"),
            "keep\n",
        )

    def test_audit_detects_overlapping_state_paths(self) -> None:
        self._write_capsule(
            state=[
                {
                    "id": "directory",
                    "path": "state",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                },
                {
                    "id": "nested",
                    "path": "state/file.bin",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                },
            ]
        )

        result = audit_capsule(capsule_path=self.capsule_path)

        self.assertFalse(result.valid)
        self.assertTrue(
            any(
                issue.code == "INVALID_PERSISTENT_STATE"
                for issue in result.issues
            )
        )

    def test_sanitized_fixture_is_valid_but_not_operational(self) -> None:
        self._write_capsule(
            state=[
                {
                    "id": "save",
                    "path": "saves/ACCOUNT_ID_REDACTED/save.dat",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                }
            ],
            sanitized=True,
        )

        result = audit_capsule(capsule_path=self.capsule_path)

        self.assertTrue(result.valid)
        self.assertFalse(result.operational)
        codes = {issue.code for issue in result.issues}
        self.assertIn("SANITIZED_FIXTURE", codes)
        self.assertIn("UNRESOLVED_STATE_PATH", codes)


if __name__ == "__main__":
    unittest.main()
