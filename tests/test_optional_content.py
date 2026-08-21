from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault.cli import build_parser
from offline_game_vault.composition import _capsule_object_digests
from offline_game_vault.optional_content import (
    OptionalContentError,
    list_optional_content,
    materialize_optional_content,
    prepare_operational_optional_content,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def make_tar(path: Path, members: dict[str, bytes]) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        directories: set[str] = set()
        for name in members:
            parts = Path(name).parts
            for index in range(1, len(parts)):
                directories.add("/".join(parts[:index]))
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return "sha256:" + digest, path.stat().st_size


class OptionalContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.collection = self.root / "collection"
        self.vault = self.collection / "01_IMMUTABLE_VAULT"
        self.vault.mkdir(parents=True)

        self.capsule_dir = self.collection / "02_CAPSULES" / "test-game"
        self.capsule_dir.mkdir(parents=True)
        self.capsule = self.capsule_dir / "capsule.json"
        self.host_contract = self.capsule_dir / "host-contracts/game-source.json"
        write_json(
            self.host_contract,
            {
                "schema": 0,
                "contract": "ogv-game-source-v1",
                "source_object": "game",
                "neutral_root": "neutral-object",
                "prefix_source": "neutral-object/payload/prefix-template",
                "game_source": "neutral-object/payload/game",
                "game_destination_in_prefix": "drive_c/Games/Test",
                "entrypoint_relative_to_game": "bin/game.exe",
                "working_directory_in_prefix": "drive_c/Games/Test",
            },
        )

        provisional = self.root / "content.tar.gz"
        digest, size = make_tar(
            provisional,
            {
                "content/DLC/file.dat": b"dlc\n",
                "content/manual/manual.pdf": b"manual\n",
            },
        )
        hexdigest = digest.removeprefix("sha256:")
        archive_path = (
            Path("objects/sha256")
            / hexdigest[:2]
            / hexdigest[2:4]
            / hexdigest
        )
        final_archive = self.vault / archive_path
        final_archive.parent.mkdir(parents=True)
        provisional.replace(final_archive)

        manifest = (
            self.vault
            / "manifests/sha256"
            / hexdigest[:2]
            / hexdigest[2:4]
            / hexdigest
        )
        manifest.parent.mkdir(parents=True)
        file_digest = hashlib.sha256(b"dlc\n").hexdigest()
        manifest.write_text(
            f"{file_digest} 4 content/DLC/file.dat\n",
            encoding="utf-8",
        )
        manifest.with_name(manifest.name + ".sha256").write_text(
            hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n",
            encoding="utf-8",
        )

        self.content_object = {
            "id": "bonus-object",
            "digest": digest,
            "archive_path": archive_path.as_posix(),
            "format": "tar.gz",
            "required": False,
            "roles": ["media"],
            "shared": False,
            "size": size,
        }
        self.base_capsule = {
            "schema": 0,
            "capsule_id": "test-game",
            "game": {
                "title": "Test",
                "source_store": "manual",
                "preserved_version": "1",
            },
            "documents": [],
            "objects": [
                {
                    "id": "game",
                    "digest": "sha256:" + "1" * 64,
                    "archive_path": "objects/sha256/11/11/" + "1" * 64,
                    "format": "tar.gz",
                    "required": True,
                    "roles": ["game_payload"],
                    "shared": False,
                    "size": 1,
                },
                self.content_object,
            ],
            "profiles": [
                {
                    "id": "game-source",
                    "platform": "linux",
                    "adapter": "other",
                    "dependencies": ["game"],
                    "host_contract": "host-contracts/game-source.json",
                    "launch": {
                        "entrypoint": "unused",
                        "working_directory": "unused",
                        "arguments": [],
                        "network": "host_default",
                    },
                }
            ],
            "optional_content": [
                {
                    "id": "dlc",
                    "object": "bonus-object",
                    "classification": "dlc",
                    "source": "content/DLC",
                    "placement": {
                        "mode": "game-overlay",
                        "destination": "DLC",
                    },
                },
                {
                    "id": "manual",
                    "object": "bonus-object",
                    "classification": "manual",
                    "source": "content/manual",
                    "placement": {
                        "mode": "sidecar",
                        "destination": "manual",
                    },
                },
            ],
        }
        write_json(self.capsule, self.base_capsule)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _operational(self) -> Path:
        path = self.root / "operational/capsule.json"
        document = json.loads(json.dumps(self.base_capsule))
        document["profiles"][0] = {
            "id": "composition-wine-test",
            "platform": "linux",
            "adapter": "wine",
            "dependencies": ["game"],
            "host_contract": "host-contracts/game-source.json",
            "launch": {
                "entrypoint": "source/payload/game/bin/game.exe",
                "working_directory": "source/payload/game",
                "arguments": [],
                "network": "host_default",
            },
        }
        write_json(path, document)
        return path

    def test_catalog_reports_object_and_manifest_availability(self) -> None:
        items = list_optional_content(
            capsule_path=self.capsule,
            collection_root=self.collection,
        )
        self.assertEqual([item["id"] for item in items], ["dlc", "manual"])
        self.assertTrue(all(item["available"] for item in items))

    def test_unselected_optional_objects_are_pruned(self) -> None:
        operational = self._operational()
        path, selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=(),
        )
        self.assertEqual(selected, ())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["id"] for item in document["objects"]],
            ["game"],
        )
        self.assertEqual(document["optional_content"], [])

    def test_selected_object_controls_manifest_preflight_digest_set(self) -> None:
        operational = self._operational()
        path, _selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=("dlc",),
        )
        digests = _capsule_object_digests(path)
        self.assertIn(self.content_object["digest"], digests)

        operational = self._operational()
        path, _selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=(),
        )
        digests = _capsule_object_digests(path)
        self.assertNotIn(self.content_object["digest"], digests)

    def test_selected_object_is_retained_once_even_for_two_items(self) -> None:
        operational = self._operational()
        path, selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=("dlc", "manual"),
        )
        self.assertEqual(
            [item.content_id for item in selected],
            ["dlc", "manual"],
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        ids = [item["id"] for item in document["objects"]]
        self.assertEqual(ids.count("bonus-object"), 1)

    def test_game_overlay_and_sidecar_are_placed_and_receipted(self) -> None:
        operational = self._operational()
        operational, selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=("dlc", "manual"),
        )

        destination = self.root / "materialized"
        game_root = destination / "source/payload/game"
        (game_root / "bin").mkdir(parents=True)
        (game_root / "bin/game.exe").write_bytes(b"exe")

        receipt = materialize_optional_content(
            source_capsule_path=self.capsule,
            source_profile_id="game-source",
            operational_capsule_path=operational,
            operational_profile_id="composition-wine-test",
            vault_root=self.vault,
            destination=destination,
            records=selected,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(
            (game_root / "DLC/file.dat").read_bytes(),
            b"dlc\n",
        )
        self.assertEqual(
            (destination / "extras/manual/manual.pdf").read_bytes(),
            b"manual\n",
        )
        data = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(data["selected_ids"], ["dlc", "manual"])

    def test_overlay_collision_is_rejected(self) -> None:
        operational = self._operational()
        operational, selected = prepare_operational_optional_content(
            source_capsule_path=self.capsule,
            operational_capsule_path=operational,
            selected_ids=("dlc",),
        )
        destination = self.root / "collision"
        game_root = destination / "source/payload/game"
        (game_root / "bin").mkdir(parents=True)
        (game_root / "bin/game.exe").write_bytes(b"exe")
        (game_root / "DLC").mkdir(parents=True)
        (game_root / "DLC/file.dat").write_bytes(b"existing")

        with self.assertRaises(OptionalContentError):
            materialize_optional_content(
                source_capsule_path=self.capsule,
                source_profile_id="game-source",
                operational_capsule_path=operational,
                operational_profile_id="composition-wine-test",
                vault_root=self.vault,
                destination=destination,
                records=selected,
            )
        self.assertEqual(
            (game_root / "DLC/file.dat").read_bytes(),
            b"existing",
        )

    def test_unknown_and_duplicate_selections_are_rejected(self) -> None:
        operational = self._operational()
        with self.assertRaises(OptionalContentError):
            prepare_operational_optional_content(
                source_capsule_path=self.capsule,
                operational_capsule_path=operational,
                selected_ids=("unknown",),
            )
        with self.assertRaises(OptionalContentError):
            prepare_operational_optional_content(
                source_capsule_path=self.capsule,
                operational_capsule_path=operational,
                selected_ids=("dlc", "dlc"),
            )

    def test_optional_object_must_not_be_a_profile_dependency(self) -> None:
        document = json.loads(self.capsule.read_text(encoding="utf-8"))
        document["profiles"][0]["dependencies"].append("bonus-object")
        write_json(self.capsule, document)
        with self.assertRaises(OptionalContentError):
            list_optional_content(
                capsule_path=self.capsule,
                collection_root=self.collection,
            )

    def test_cli_registers_catalog_and_repeatable_selection(self) -> None:
        parser = build_parser()
        list_args = parser.parse_args(
            [
                "list-optional-content",
                "--collection-root",
                str(self.collection),
                "--capsule",
                str(self.capsule),
                "--json",
            ]
        )
        self.assertTrue(list_args.json)

        compose_args = parser.parse_args(
            [
                "compose",
                "--collection-root",
                str(self.collection),
                "--capsule",
                str(self.capsule),
                "--backend",
                "direct-wine",
                "--runner",
                "runner",
                "--content-id",
                "dlc",
                "--content-id",
                "manual",
                "--destination",
                str(self.root / "out"),
            ]
        )
        self.assertEqual(compose_args.content_id, ["dlc", "manual"])

    def test_capsule_schema_exposes_optional_content(self) -> None:
        schema = json.loads(
            Path("schemas/capsule.schema.json").read_text(encoding="utf-8")
        )
        optional = schema["properties"]["optional_content"]
        placement = optional["items"]["properties"]["placement"]
        self.assertEqual(
            placement["properties"]["mode"]["enum"],
            ["game-overlay", "sidecar"],
        )


if __name__ == "__main__":
    unittest.main()
