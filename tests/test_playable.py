from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from offline_game_vault.playable import (
    PlayableError,
    materialize_playable_profile,
    remove_playable_profile,
    run_playable_profile,
    verify_playable_profile,
)
from offline_game_vault.state_manager import preserve_state
from offline_game_vault.storage import ingest_object
from offline_game_vault.verifier import ObjectSpec


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def object_archive_path(digest: str) -> str:
    hexadecimal = digest.removeprefix("sha256:")
    return (
        f"objects/sha256/{hexadecimal[:2]}/"
        f"{hexadecimal[2:4]}/{hexadecimal}"
    )


def make_tar(
    path: Path,
    entries: dict[str, tuple[bytes, int]],
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, (payload, mode) in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            archive.addfile(member, io.BytesIO(payload))


class PlayableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.destination = self.root / "playable"
        self.state_source = self.root / "state-source"
        self.state_source.mkdir()
        self.backup = self.root / "accepted-state"

        self.game_payload = b"synthetic-game"
        self.original_payload = b"synthetic-original"
        self.active_dll = b"synthetic-active-dll"
        self.original_dll = b"synthetic-original-dll"
        self.accepted_save = b"accepted-save"
        self.accepted_identity = b"accepted-identity"

        wine_script = b"""#!/bin/sh
set -eu
printf x >> "$WINEPREFIX/drive_c/users/test/save.dat"
exit 0
"""
        wineserver_script = b"""#!/bin/sh
exit 0
"""

        self.game_archive = self.root / "game.tar.gz"
        self.runner_archive = self.root / "runner.tar.gz"
        make_tar(
            self.game_archive,
            {
                "Bottle/drive_c/Game/game.exe": (
                    self.game_payload,
                    0o644,
                ),
                "Bottle/drive_c/Game/game_ORIGINAL.exe": (
                    self.original_payload,
                    0o644,
                ),
                "Bottle/drive_c/Game/steam_api64.dll": (
                    self.active_dll,
                    0o644,
                ),
                "Bottle/drive_c/Game/steam_api64.dll.gbe_backup": (
                    self.original_dll,
                    0o644,
                ),
                "Bottle/drive_c/users/test/save.dat": (
                    b"baseline-save",
                    0o600,
                ),
                (
                    "Bottle/drive_c/users/test/"
                    "GSE Saves/settings/configs.user.ini"
                ): (
                    b"baseline-identity",
                    0o600,
                ),
            },
        )
        make_tar(
            self.runner_archive,
            {
                "runner/files/bin/wine": (
                    wine_script,
                    0o755,
                ),
                "runner/files/bin/wineserver": (
                    wineserver_script,
                    0o755,
                ),
            },
        )

        self.game_digest = digest_file(self.game_archive)
        self.runner_digest = digest_file(self.runner_archive)

        self.capsule = {
            "schema": 0,
            "capsule_id": "playable-test",
            "game": {
                "title": "Synthetic",
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
                    "id": "game",
                    "digest": self.game_digest,
                    "roles": [
                        "game_payload",
                        "prefix_baseline",
                    ],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": object_archive_path(
                        self.game_digest
                    ),
                    "shared": False,
                },
                {
                    "id": "runner",
                    "digest": self.runner_digest,
                    "roles": ["runner"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": object_archive_path(
                        self.runner_digest
                    ),
                    "shared": True,
                },
            ],
            "persistent_state": [
                {
                    "id": "save",
                    "path": "drive_c/users/test/save.dat",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                },
                {
                    "id": "identity",
                    "path": (
                        "drive_c/users/test/"
                        "GSE Saves/settings/configs.user.ini"
                    ),
                    "kind": "identity",
                    "backup": True,
                    "sensitive": True,
                    "required": True,
                },
            ],
            "profiles": [
                {
                    "id": "linux-direct-wine",
                    "platform": "linux",
                    "adapter": "wine",
                    "status": "verified",
                    "dependencies": ["game", "runner"],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": (
                            "prefix/drive_c/Game/game.exe"
                        ),
                        "working_directory": (
                            "prefix/drive_c/Game"
                        ),
                        "arguments": [],
                        "network": "host_default",
                        "environment": {
                            "WINEDEBUG": "-all",
                        },
                    },
                    "playable": {
                        "schema": 0,
                        "backend": "wine",
                        "paths": {
                            "prefix": "prefix",
                            "runner": "runner/runner",
                            "wine": (
                                "runner/runner/files/bin/wine"
                            ),
                            "wineserver": (
                                "runner/runner/files/bin/wineserver"
                            ),
                            "runtime": "runtime",
                            "launcher": "play.sh",
                            "uninstaller": "uninstall.sh",
                        },
                        "layout": [
                            {
                                "object": "game",
                                "source": "Bottle",
                                "destination": "prefix",
                            },
                            {
                                "object": "runner",
                                "source": "runner",
                                "destination": "runner/runner",
                            },
                        ],
                        "prefix_operations": [
                            {
                                "type": "mkdir",
                                "path": "prefix/dosdevices",
                            },
                            {
                                "type": "symlink",
                                "path": "prefix/dosdevices/c:",
                                "target": "../drive_c",
                            },
                        ],
                        "protected_files": [
                            {
                                "path": (
                                    "prefix/drive_c/Game/game.exe"
                                ),
                                "digest": digest_bytes(
                                    self.game_payload
                                ),
                                "size": len(self.game_payload),
                            },
                            {
                                "path": (
                                    "prefix/drive_c/Game/"
                                    "game_ORIGINAL.exe"
                                ),
                                "digest": digest_bytes(
                                    self.original_payload
                                ),
                                "size": len(
                                    self.original_payload
                                ),
                            },
                            {
                                "path": (
                                    "prefix/drive_c/Game/"
                                    "steam_api64.dll"
                                ),
                                "digest": digest_bytes(
                                    self.active_dll
                                ),
                                "size": len(self.active_dll),
                            },
                            {
                                "path": (
                                    "prefix/drive_c/Game/"
                                    "steam_api64.dll.gbe_backup"
                                ),
                                "digest": digest_bytes(
                                    self.original_dll
                                ),
                                "size": len(
                                    self.original_dll
                                ),
                            },
                        ],
                    },
                    "acceptance_report": "acceptance.json",
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self._write_capsule()
        for name in (
            "README.md",
            "GAME.md",
            "CREDITS.md",
            "PRESERVED.md",
        ):
            (self.fixture / name).write_text(
                "fixture\n",
                encoding="utf-8",
            )
        (self.fixture / "host-contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        (self.fixture / "acceptance.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        self._ingest(
            "game",
            self.game_archive,
            self.game_digest,
        )
        self._ingest(
            "runner",
            self.runner_archive,
            self.runner_digest,
        )

        save = (
            self.state_source
            / "drive_c/users/test/save.dat"
        )
        identity = (
            self.state_source
            / "drive_c/users/test/"
            "GSE Saves/settings/configs.user.ini"
        )
        save.parent.mkdir(parents=True, exist_ok=True)
        identity.parent.mkdir(parents=True, exist_ok=True)
        save.write_bytes(self.accepted_save)
        identity.write_bytes(self.accepted_identity)
        os.chmod(save, 0o600)
        os.chmod(identity, 0o600)

        preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_source,
            backup=self.backup,
            confirm_stopped=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_capsule(self) -> None:
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def _ingest(
        self,
        object_id: str,
        source: Path,
        expected_digest: str,
    ) -> None:
        hexadecimal = expected_digest.removeprefix(
            "sha256:"
        )
        destination = (
            self.vault
            / "objects"
            / "sha256"
            / hexadecimal[:2]
            / hexadecimal[2:4]
            / hexadecimal
        )
        ingest_object(
            source=source,
            destination_spec=ObjectSpec(
                object_id=object_id,
                path=destination,
                expected_digest=expected_digest,
                expected_size=None,
                vault_root=self.vault.resolve(),
            ),
        )

    def _materialize(self):
        return materialize_playable_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-direct-wine",
            vault_root=self.vault,
            destination=self.destination,
            state_backup=self.backup,
        )

    def test_materializes_restores_and_validates_receipt(self) -> None:
        result = self._materialize()

        self.assertTrue(result.complete)
        self.assertFalse(result.reused)
        self.assertEqual(result.object_count, 2)
        self.assertEqual(result.protected_file_count, 6)
        self.assertEqual(result.state_item_count, 2)
        self.assertEqual(
            (
                self.destination
                / "prefix/drive_c/users/test/save.dat"
            ).read_bytes(),
            self.accepted_save,
        )
        self.assertEqual(
            os.readlink(
                self.destination
                / "prefix/dosdevices/c:"
            ),
            "../drive_c",
        )
        self.assertFalse(
            (
                self.destination
                / "prefix/dosdevices/z:"
            ).exists()
        )
        self.assertTrue(
            (
                self.destination / "play.sh"
            ).is_file()
        )
        self.assertTrue(
            (
                self.destination / "uninstall.sh"
            ).is_file()
        )

        receipt = json.loads(
            (
                self.destination
                / "playable-materialization.json"
            ).read_text(encoding="utf-8")
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/"
                "playable-materialization-receipt.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(receipt)
        )
        self.assertEqual(errors, [])

        verification = verify_playable_profile(
            destination=self.destination
        )
        self.assertTrue(verification.verified)

    def test_second_materialization_reuses_valid_destination(self) -> None:
        first = self._materialize()
        second = self._materialize()

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.receipt_id, second.receipt_id)

    def test_play_writes_receipt_and_changes_save(self) -> None:
        self._materialize()
        result = run_playable_profile(
            destination=self.destination
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.game_process_rc, 0)
        self.assertEqual(result.wineserver_wait_rc, 0)
        self.assertEqual(
            (
                self.destination
                / "prefix/drive_c/users/test/save.dat"
            ).read_bytes(),
            self.accepted_save + b"x",
        )
        receipt = json.loads(
            (
                self.destination
                / "receipts/last-play.json"
            ).read_text(encoding="utf-8")
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/play-receipt.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(receipt)
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            receipt["startup_window_ready_status"],
            "not_instrumented",
        )

    def test_changed_state_aborts_default_removal(self) -> None:
        self._materialize()
        run_playable_profile(destination=self.destination)

        with self.assertRaisesRegex(
            PlayableError,
            "Persistent state changed",
        ):
            remove_playable_profile(
                destination=self.destination
            )

        self.assertTrue(self.destination.is_dir())

    def test_changed_state_exports_and_removes(self) -> None:
        self._materialize()
        run_playable_profile(destination=self.destination)
        export = self.root / "state-export"

        result = remove_playable_profile(
            destination=self.destination,
            export_state=export,
        )

        self.assertTrue(result.removed)
        self.assertTrue(result.state_exported)
        self.assertTrue(result.changed_state_detected)
        self.assertFalse(self.destination.exists())
        document = json.loads(
            (
                export / "state-export.json"
            ).read_text(encoding="utf-8")
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/state-export.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(document)
        )
        self.assertEqual(errors, [])
        payload = (
            export / "payload/0001-save/data"
        )
        self.assertEqual(
            payload.read_bytes(),
            self.accepted_save + b"x",
        )

    def test_directory_state_is_restored_detected_and_exported(self) -> None:
        self.capsule["persistent_state"].append(
            {
                "id": "configuration",
                "path": "drive_c/users/test/config",
                "kind": "configuration",
                "backup": True,
                "sensitive": False,
                "required": True,
            }
        )
        self._write_capsule()
        configuration = (
            self.state_source
            / "drive_c/users/test/config"
        )
        configuration.mkdir(parents=True)
        (configuration / "settings.ini").write_text(
            "value=1\n",
            encoding="utf-8",
        )
        nested = configuration / "nested"
        nested.mkdir()
        (nested / "data.bin").write_bytes(b"data")
        backup = self.root / "accepted-directory-state"
        preserve_state(
            capsule_path=self.capsule_path,
            state_root=self.state_source,
            backup=backup,
            confirm_stopped=True,
        )
        destination = self.root / "playable-directory-state"
        result = materialize_playable_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-direct-wine",
            vault_root=self.vault,
            destination=destination,
            state_backup=backup,
        )
        self.assertEqual(result.state_item_count, 3)
        restored = (
            destination
            / "prefix/drive_c/users/test/config"
        )
        self.assertEqual(
            (restored / "settings.ini").read_text(
                encoding="utf-8"
            ),
            "value=1\n",
        )
        (restored / "settings.ini").write_text(
            "value=2\n",
            encoding="utf-8",
        )
        export = self.root / "directory-state-export"
        removal = remove_playable_profile(
            destination=destination,
            export_state=export,
        )
        self.assertTrue(removal.changed_state_detected)
        self.assertFalse(destination.exists())
        exported = json.loads(
            (
                export / "state-export.json"
            ).read_text(encoding="utf-8")
        )
        configuration_item = next(
            item
            for item in exported["items"]
            if item["id"] == "configuration"
        )
        self.assertEqual(
            configuration_item["entry_type"],
            "directory",
        )
        payload = export.joinpath(
            *Path(
                configuration_item["payload_path"]
            ).parts
        )
        self.assertEqual(
            (payload / "settings.ini").read_text(
                encoding="utf-8"
            ),
            "value=2\n",
        )

    def test_missing_state_backup_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            PlayableError,
            "--state-backup is required",
        ):
            materialize_playable_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-direct-wine",
                vault_root=self.vault,
                destination=self.destination,
            )
        self.assertFalse(self.destination.exists())

    def test_protected_file_mismatch_rolls_back(self) -> None:
        self.capsule["profiles"][0]["playable"][
            "protected_files"
        ][0]["digest"] = digest_bytes(b"wrong")
        self._write_capsule()

        with self.assertRaisesRegex(
            PlayableError,
            "Protected file failed verification",
        ):
            self._materialize()

        self.assertFalse(self.destination.exists())
        self.assertEqual(
            list(self.root.glob(".ogv-playable-*")),
            [],
        )

    def test_isolated_network_claim_is_rejected(self) -> None:
        self.capsule["profiles"][0]["launch"][
            "network"
        ] = "isolated"
        self._write_capsule()

        with self.assertRaisesRegex(
            PlayableError,
            "does not yet implement an isolated network",
        ):
            self._materialize()

    def test_unmapped_dependency_is_rejected(self) -> None:
        self.capsule["profiles"][0]["playable"][
            "layout"
        ] = self.capsule["profiles"][0][
            "playable"
        ]["layout"][:1]
        self._write_capsule()

        with self.assertRaisesRegex(
            PlayableError,
            "map every profile dependency",
        ):
            self._materialize()

    def test_unrecognized_existing_destination_is_rejected(self) -> None:
        self.destination.mkdir()
        (self.destination / "keep.txt").write_text(
            "keep",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            PlayableError,
            "not a valid playable materialization",
        ):
            self._materialize()

        self.assertEqual(
            (self.destination / "keep.txt").read_text(
                encoding="utf-8"
            ),
            "keep",
        )


if __name__ == "__main__":
    unittest.main()
