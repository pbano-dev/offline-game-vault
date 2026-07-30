from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:  # optional test dependency
    Draft202012Validator = None

from offline_game_vault.storage import (
    canonical_object_path,
    ingest_object,
)
from offline_game_vault.umu_adapter import (
    UmuAdapterError,
    materialize_umu_profile,
    remove_umu_materialization,
    run_umu_materialization,
    verify_umu_materialization,
)
from offline_game_vault.verifier import ObjectSpec


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_tar(
    path: Path,
    entries: dict[str, tuple[bytes, int]],
    directories: tuple[str, ...] = (),
) -> None:
    with tarfile.open(path, "w") as archive:
        for directory in directories:
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, (payload, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            archive.addfile(info, io.BytesIO(payload))


class UmuAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("tar") is None:
            self.skipTest("GNU tar is unavailable")

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = self.root / "capsule"
        self.fixture.mkdir()
        (self.fixture / "launchers").mkdir()
        (self.fixture / "manifests").mkdir()
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.destination = self.root / "materialized"

        self.prefix_archive = self.root / "prefix.tar"
        make_tar(
            self.prefix_archive,
            {
                "payload/prefix/base.txt": (b"baseline\n", 0o600),
            },
        )
        self.prefix_digest = sha256_file(self.prefix_archive)

        self.game_archive = self.root / "game.tar"
        make_tar(
            self.game_archive,
            {
                "payload/game/game.bin": (b"game\n", 0o644),
                "baseline/prefix.tar": (
                    self.prefix_archive.read_bytes(),
                    0o600,
                ),
            },
        )
        self.game_digest = sha256_file(self.game_archive)

        self.umu_archive = self.root / "umu.tar"
        make_tar(
            self.umu_archive,
            {
                "engine/tool.txt": (b"umu\n", 0o644),
            },
            directories=("engine/runtime", "engine/runtime/var"),
        )
        self.umu_digest = sha256_file(self.umu_archive)

        self.config_archive = self.state_root / "config.tar"
        make_tar(
            self.config_archive,
            {
                "payload/game/config.ini": (b"required=1\n", 0o600),
            },
        )
        self.config_digest = sha256_file(self.config_archive)

        self.save_archive = self.state_root / "save.tar"
        make_tar(
            self.save_archive,
            {
                "payload/prefix/save.bin": (b"accepted", 0o600),
            },
        )
        self.save_digest = sha256_file(self.save_archive)

        launcher = self.fixture / "launchers/run.sh"
        launcher.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf x >> payload/prefix/save.bin\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        sanitizer = self.fixture / "launchers/sanitize.sh"
        sanitizer.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "rm -rf engine/runtime/var\n"
            "mkdir -m 0755 engine/runtime/var\n",
            encoding="utf-8",
        )
        sanitizer.chmod(0o755)

        manifests = {
            "game.sha256": (
                f"{sha256_bytes(b'game\n')}  payload/game/game.bin\n"
            ),
            "prefix.sha256": (
                f"{sha256_bytes(b'baseline\n')}  "
                "payload/prefix/base.txt\n"
            ),
            "umu.sha256": (
                f"{sha256_bytes(b'umu\n')}  engine/tool.txt\n"
            ),
            "config.sha256": (
                f"{sha256_bytes(b'required=1\n')}  "
                "payload/game/config.ini\n"
            ),
            "save.sha256": (
                f"{sha256_bytes(b'accepted')}  payload/prefix/save.bin\n"
            ),
        }
        for name, content in manifests.items():
            (self.fixture / "manifests" / name).write_text(
                content,
                encoding="utf-8",
            )

        game_declared = "sha256:" + self.game_digest
        umu_declared = "sha256:" + self.umu_digest
        game_path = canonical_object_path(self.vault, game_declared)
        umu_path = canonical_object_path(self.vault, umu_declared)

        ingest_object(
            source=self.game_archive,
            destination_spec=ObjectSpec(
                object_id="game",
                path=game_path,
                expected_digest=game_declared,
                expected_size=self.game_archive.stat().st_size,
                vault_root=self.vault.resolve(),
            ),
        )
        ingest_object(
            source=self.umu_archive,
            destination_spec=ObjectSpec(
                object_id="umu-stack",
                path=umu_path,
                expected_digest=umu_declared,
                expected_size=self.umu_archive.stat().st_size,
                vault_root=self.vault.resolve(),
            ),
        )

        def archive_path(digest: str) -> str:
            value = digest.removeprefix("sha256:")
            return (
                f"objects/sha256/{value[:2]}/{value[2:4]}/{value}"
            )

        self.capsule = {
            "schema": 0,
            "capsule_id": "umu-test",
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
                    "digest": game_declared,
                    "size": self.game_archive.stat().st_size,
                    "roles": ["game_payload", "prefix_baseline"],
                    "format": "tar",
                    "required": True,
                    "archive_path": archive_path(game_declared),
                },
                {
                    "id": "umu-stack",
                    "digest": umu_declared,
                    "size": self.umu_archive.stat().st_size,
                    "roles": ["runner", "runtime"],
                    "format": "tar",
                    "required": True,
                    "archive_path": archive_path(umu_declared),
                    "shared": True,
                },
            ],
            "persistent_state": [],
            "profiles": [
                {
                    "id": "linux-umu",
                    "platform": "linux",
                    "adapter": "umu",
                    "status": "verified",
                    "dependencies": ["game", "umu-stack"],
                    "host_contract": "host.json",
                    "launch": {
                        "entrypoint": "payload/game/game.bin",
                        "network": "host_default",
                    },
                    "umu": {
                        "schema": 0,
                        "layout": [
                            {
                                "object": "game",
                                "source": "payload",
                                "destination": "payload",
                            },
                            {
                                "object": "game",
                                "source": "baseline",
                                "destination": "baseline",
                            },
                            {
                                "object": "umu-stack",
                                "source": "engine",
                                "destination": "engine",
                            },
                        ],
                        "nested_archives": [
                            {
                                "path": "baseline/prefix.tar",
                                "digest": "sha256:" + self.prefix_digest,
                                "destination": ".",
                                "remove_after": True,
                            }
                        ],
                        "state_archives": [
                            {
                                "id": "required-config",
                                "filename": "config.tar",
                                "digest": "sha256:" + self.config_digest,
                                "policy": "always",
                            },
                            {
                                "id": "main",
                                "filename": "save.tar",
                                "digest": "sha256:" + self.save_digest,
                                "policy": "selectable",
                            },
                        ],
                        "launchers": [
                            {
                                "source": "launchers/run.sh",
                                "destination": "run.sh",
                                "digest": (
                                    "sha256:" + sha256_file(launcher)
                                ),
                                "mode": 0o755,
                            },
                            {
                                "source": "launchers/sanitize.sh",
                                "destination": "sanitize.sh",
                                "digest": (
                                    "sha256:" + sha256_file(sanitizer)
                                ),
                                "mode": 0o755,
                            },
                        ],
                        "protected_manifests": [
                            {"source": "manifests/game.sha256"},
                            {"source": "manifests/prefix.sha256"},
                            {"source": "manifests/umu.sha256"},
                            {"source": "manifests/config.sha256"},
                            {
                                "source": "manifests/save.sha256",
                                "when_save": "main",
                            },
                        ],
                        "symlink_manifests": [],
                        "allowed_absolute_symlinks": [],
                        "mutable_paths": ["payload/prefix/save.bin"],
                        "paths": {
                            "launcher": "run.sh",
                            "sanitizer": "sanitize.sh",
                            "runtime_var": "engine/runtime/var",
                        },
                    },
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is not installed",
    )
    def test_capsule_schema_accepts_umu_contract(self) -> None:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/capsule.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(schema).iter_errors(self.capsule)
        )
        self.assertEqual(errors, [])

    def test_materialize_run_verify_and_remove(self) -> None:
        materialized = materialize_umu_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-umu",
            vault_root=self.vault,
            state_root=self.state_root,
            destination=self.destination,
            save_id="main",
        )
        self.assertTrue(materialized.complete)
        self.assertEqual(materialized.selected_save, "main")
        self.assertEqual(
            (self.destination / "payload/prefix/save.bin").read_bytes(),
            b"accepted",
        )

        verified = verify_umu_materialization(
            destination=self.destination
        )
        self.assertTrue(verified.verified)

        played = run_umu_materialization(
            destination=self.destination
        )
        self.assertTrue(played.complete)
        self.assertEqual(
            (self.destination / "payload/prefix/save.bin").read_bytes(),
            b"acceptedx",
        )

        with self.assertRaisesRegex(
            UmuAdapterError,
            "must be preserved",
        ):
            remove_umu_materialization(
                destination=self.destination,
                confirm_state_preserved=False,
            )

        removed = remove_umu_materialization(
            destination=self.destination,
            confirm_state_preserved=True,
        )
        self.assertTrue(removed.removed)
        self.assertFalse(self.destination.exists())

    def test_clean_materialization_omits_selectable_save(self) -> None:
        result = materialize_umu_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-umu",
            vault_root=self.vault,
            state_root=self.state_root,
            destination=self.destination,
            save_id=None,
        )
        self.assertTrue(result.complete)
        self.assertFalse(
            (self.destination / "payload/prefix/save.bin").exists()
        )
        self.assertTrue(
            (self.destination / "payload/game/config.ini").is_file()
        )


if __name__ == "__main__":
    unittest.main()
