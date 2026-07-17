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

from offline_game_vault.materializer import (
    MaterializationError,
    materialize_profile,
    remove_materialization,
)
from offline_game_vault.storage import ingest_object
from offline_game_vault.verifier import ObjectSpec


def make_tar(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(member, io.BytesIO(payload))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def archive_path(value: str) -> str:
    hexadecimal = value.removeprefix("sha256:")
    return (
        f"objects/sha256/{hexadecimal[:2]}/"
        f"{hexadecimal[2:4]}/{hexadecimal}"
    )


class MaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.destination = self.root / "materialized"

        self.game_archive = self.root / "game.tar.gz"
        self.runner_archive = self.root / "runner.tar.gz"
        make_tar(
            self.game_archive,
            {
                "drive_c/Game/game.exe": b"game",
                "drive_c/users/user/save.dat": b"save",
            },
        )
        make_tar(
            self.runner_archive,
            {
                "runner/proton": b"runner",
                "runner/files/bin/tool.sh": b"#!/bin/sh\n",
            },
        )

        self.game_digest = digest(self.game_archive)
        self.runner_digest = digest(self.runner_archive)

        self.capsule = {
            "schema": 0,
            "capsule_id": "materializer-test",
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
                    "id": "game",
                    "digest": self.game_digest,
                    "roles": ["game_payload"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": archive_path(self.game_digest),
                    "shared": False,
                },
                {
                    "id": "runner",
                    "digest": self.runner_digest,
                    "roles": ["runner"],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": archive_path(self.runner_digest),
                    "shared": True,
                },
            ],
            "persistent_state": [
                {
                    "id": "save",
                    "path": "drive_c/users/user/save.dat",
                    "kind": "save",
                    "backup": True,
                    "sensitive": True,
                }
            ],
            "profiles": [
                {
                    "id": "linux-test",
                    "platform": "linux",
                    "adapter": "bottles",
                    "status": "candidate",
                    "dependencies": ["game", "runner"],
                    "host_contract": "host-contract.json",
                    "launch": {
                        "entrypoint": "drive_c/Game/game.exe",
                        "network": "isolated",
                    },
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )
        (self.fixture / "host-contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        self._ingest("game", self.game_archive, self.game_digest)
        self._ingest(
            "runner",
            self.runner_archive,
            self.runner_digest,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(
        self,
        object_id: str,
        source: Path,
        expected_digest: str,
    ) -> None:
        hexadecimal = expected_digest.removeprefix("sha256:")
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

    def test_materializes_complete_profile_and_receipt(self) -> None:
        vault_before = {
            path.relative_to(self.vault).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.vault.rglob("*")
            if path.is_file()
        }

        result = materialize_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-test",
            vault_root=self.vault,
            destination=self.destination,
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.object_count, 2)
        self.assertTrue(
            (
                self.destination
                / "objects/game/drive_c/Game/game.exe"
            ).is_file()
        )
        self.assertTrue(
            (
                self.destination
                / "objects/runner/runner/proton"
            ).is_file()
        )

        receipt_path = (
            self.destination / "materialization-receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["destination"], ".")
        self.assertEqual(receipt["capsule_id"], "materializer-test")
        self.assertEqual(
            receipt["persistent_state"][0]["id"],
            "save",
        )
        self.assertNotIn(str(self.root), receipt_path.read_text())

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas/receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(receipt)
        )
        self.assertEqual(errors, [])

        vault_after = {
            path.relative_to(self.vault).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(vault_before, vault_after)

        self.assertEqual(
            list(self.destination.parent.glob(".ogv-stage-*")),
            [],
        )
        self.assertEqual(
            list(self.destination.parent.glob(".ogv-lock-*")),
            [],
        )

    def test_existing_destination_is_rejected_without_changes(self) -> None:
        self.destination.mkdir()
        marker = self.destination / "marker"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(
            MaterializationError,
            "already exists",
        ):
            materialize_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-test",
                vault_root=self.vault,
                destination=self.destination,
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_destination_inside_vault_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            MaterializationError,
            "outside the immutable vault",
        ):
            materialize_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-test",
                vault_root=self.vault,
                destination=self.vault / "working",
            )

    def test_failed_extraction_does_not_publish_destination(self) -> None:
        malicious = self.root / "malicious.tar.gz"
        with tarfile.open(malicious, "w:gz") as archive:
            member = tarfile.TarInfo("../escape")
            payload = b"escape"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

        malicious_digest = digest(malicious)
        self.capsule["objects"][0]["digest"] = malicious_digest
        self.capsule["objects"][0]["archive_path"] = archive_path(
            malicious_digest
        )
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )
        self._ingest("game", malicious, malicious_digest)

        with self.assertRaisesRegex(
            MaterializationError,
            "path traversal",
        ):
            materialize_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-test",
                vault_root=self.vault,
                destination=self.destination,
            )

        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(
            list(self.root.glob(".ogv-stage-*")),
            [],
        )
        self.assertEqual(
            list(self.root.glob(".ogv-lock-*")),
            [],
        )

    def test_removal_refuses_unpreserved_state(self) -> None:
        materialize_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-test",
            vault_root=self.vault,
            destination=self.destination,
        )

        with self.assertRaisesRegex(
            MaterializationError,
            "Persistent state must be preserved",
        ):
            remove_materialization(
                destination=self.destination,
                confirm_state_preserved=False,
            )

        self.assertTrue(self.destination.exists())

    def test_removal_after_state_confirmation(self) -> None:
        materialize_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-test",
            vault_root=self.vault,
            destination=self.destination,
        )

        result = remove_materialization(
            destination=self.destination,
            confirm_state_preserved=True,
        )

        self.assertTrue(result.removed)
        self.assertFalse(self.destination.exists())
        self.assertEqual(
            list(self.root.glob(".ogv-remove-*")),
            [],
        )
        self.assertEqual(
            list(self.root.glob(".ogv-lock-*")),
            [],
        )

    def test_removal_refuses_unknown_top_level_path(self) -> None:
        materialize_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-test",
            vault_root=self.vault,
            destination=self.destination,
        )
        (self.destination / "unknown.txt").write_text(
            "do not delete",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MaterializationError,
            "not declared safe to remove",
        ):
            remove_materialization(
                destination=self.destination,
                confirm_state_preserved=True,
            )

        self.assertTrue(self.destination.exists())
        self.assertTrue(
            (self.destination / "unknown.txt").exists()
        )


if __name__ == "__main__":
    unittest.main()
