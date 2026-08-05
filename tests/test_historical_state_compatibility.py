from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from offline_game_vault.composition_state import (
    prepare_composition_state,
)
from offline_game_vault.state_manager import (
    BACKUP_RECEIPT_NAME,
    StateDeclaration,
    StateError,
    _backup_declarations_for_receipt,
    preserve_state,
    restore_state,
    verify_state_backup,
)


def _declaration(
    item_id: str,
    *,
    path: str | None = None,
    required: bool = False,
) -> StateDeclaration:
    return StateDeclaration(
        id=item_id,
        path=path or f"drive_c/state/{item_id}",
        kind="save",
        backup=True,
        sensitive=True,
        required=required,
    )


def _receipt_item(
    declaration: StateDeclaration,
    *,
    required: bool | None = None,
    path: str | None = None,
) -> dict[str, object]:
    return {
        "id": declaration.id,
        "declared_path": path or declaration.path,
        "kind": declaration.kind,
        "sensitive": declaration.sensitive,
        "required": (
            declaration.required
            if required is None
            else required
        ),
    }


class HistoricalCompatibilityUnitTests(unittest.TestCase):
    def test_new_optional_item_and_relaxed_required_are_safe(
        self,
    ) -> None:
        old_save = _declaration("save", required=True)
        current = (
            _declaration("identity", required=True),
            _declaration("save", required=False),
            _declaration("save-backup", required=False),
        )
        document = {
            "state_definition_digest": "sha256:" + "a" * 64,
            "items": [
                _receipt_item(current[0]),
                _receipt_item(old_save),
            ],
        }

        selected = _backup_declarations_for_receipt(
            document=document,
            declarations=current,
            definition_digest="sha256:" + "b" * 64,
        )

        self.assertEqual(
            [declaration.id for declaration in selected],
            ["identity", "save"],
        )
        self.assertTrue(selected[1].required)

    def test_changed_path_is_rejected(self) -> None:
        current = (_declaration("save"),)
        document = {
            "state_definition_digest": "sha256:" + "a" * 64,
            "items": [
                _receipt_item(
                    current[0],
                    path="drive_c/other/save",
                )
            ],
        }

        with self.assertRaisesRegex(
            StateError,
            "does not match the current capsule declared_path",
        ):
            _backup_declarations_for_receipt(
                document=document,
                declarations=current,
                definition_digest="sha256:" + "b" * 64,
            )

    def test_new_required_item_is_rejected(self) -> None:
        old = _declaration("save", required=True)
        current = (
            old,
            _declaration("identity", required=True),
        )
        document = {
            "state_definition_digest": "sha256:" + "a" * 64,
            "items": [_receipt_item(old)],
        }

        with self.assertRaisesRegex(
            StateError,
            "omits state now required",
        ):
            _backup_declarations_for_receipt(
                document=document,
                declarations=current,
                definition_digest="sha256:" + "b" * 64,
            )


class HistoricalCompatibilityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        self.state_root = self.root / "state"
        self.fixture.mkdir()
        self.state_root.mkdir()
        self.capsule = self.fixture / "capsule.json"

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

    def _write_capsule(
        self,
        state: list[dict[str, object]],
    ) -> None:
        document = {
            "schema": 0,
            "capsule_id": "historical-state-test",
            "game": {
                "title": "Historical State Test",
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
                    "dependencies": ["payload"],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": "game.exe",
                        "network": "isolated",
                    },
                }
            ],
        }
        self.capsule.write_text(
            json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_historical_subset_verifies_and_restores(
        self,
    ) -> None:
        old_state = [
            {
                "id": "identity",
                "path": "identity/config.ini",
                "kind": "identity",
                "backup": True,
                "sensitive": True,
                "required": True,
            },
            {
                "id": "save",
                "path": "save/S0000.sl2",
                "kind": "save",
                "backup": True,
                "sensitive": True,
                "required": True,
            },
        ]
        self._write_capsule(old_state)

        identity = self.state_root / "identity/config.ini"
        identity.parent.mkdir(parents=True)
        identity.write_text(
            "preserved-identity\n",
            encoding="utf-8",
        )
        save = self.state_root / "save/S0000.sl2"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"preserved-save")

        backup = self.root / "historical-backup"
        preserve_state(
            capsule_path=self.capsule,
            state_root=self.state_root,
            backup=backup,
            confirm_stopped=True,
        )

        receipt = json.loads(
            (backup / BACKUP_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(receipt["items"]), 2)

        current_state = [
            old_state[0],
            old_state[1] | {"required": False},
            {
                "id": "save-backup",
                "path": "save/S0000.sl2.bak",
                "kind": "save",
                "backup": True,
                "sensitive": True,
                "required": False,
            },
        ]
        self._write_capsule(current_state)

        verification = verify_state_backup(
            capsule_path=self.capsule,
            backup=backup,
        )
        self.assertTrue(
            verification.verified,
            verification.problems,
        )
        self.assertEqual(verification.item_count, 2)
        self.assertEqual(verification.present_count, 2)

        selection = prepare_composition_state(
            capsule_path=self.capsule,
            state_backup=backup,
            require_declared_state=True,
        )
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.backup_id, verification.backup_id)
        self.assertEqual(selection.item_count, 2)

        identity.write_text(
            "current-identity\n",
            encoding="utf-8",
        )
        save.write_bytes(b"current-save")
        companion = self.state_root / "save/S0000.sl2.bak"
        companion.write_bytes(b"keep-current-companion")

        restored = restore_state(
            capsule_path=self.capsule,
            state_root=self.state_root,
            backup=backup,
            snapshot=self.root / "snapshot",
            confirm_stopped=True,
        )

        self.assertTrue(restored.complete)
        self.assertEqual(restored.item_count, 2)
        self.assertEqual(restored.restored_count, 2)
        self.assertEqual(
            identity.read_text(encoding="utf-8"),
            "preserved-identity\n",
        )
        self.assertEqual(save.read_bytes(), b"preserved-save")
        self.assertEqual(
            companion.read_bytes(),
            b"keep-current-companion",
        )


if __name__ == "__main__":
    unittest.main()
