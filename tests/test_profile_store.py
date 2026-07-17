from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.profile_store import (
    ProfileStoreError,
    ingest_profile,
    parse_source_assignments,
    verify_profile,
)


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def archive_path(value: str) -> str:
    hexadecimal = value.removeprefix("sha256:")
    return (
        f"objects/sha256/{hexadecimal[:2]}/"
        f"{hexadecimal[2:4]}/{hexadecimal}"
    )


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()

        self.payload_a = b"profile object A\n"
        self.payload_b = b"profile object B\n"
        self.digest_a = digest(self.payload_a)
        self.digest_b = digest(self.payload_b)

        self.source_a = self.root / "source-a.tar.gz"
        self.source_b = self.root / "source-b.tar.gz"
        self.source_a.write_bytes(self.payload_a)
        self.source_b.write_bytes(self.payload_b)

        self.capsule = {
            "capsule_id": "example-capsule",
            "objects": [
                {
                    "id": "object-a",
                    "digest": self.digest_a,
                    "format": "tar.gz",
                    "archive_path": archive_path(self.digest_a),
                },
                {
                    "id": "object-b",
                    "digest": self.digest_b,
                    "format": "tar.gz",
                    "archive_path": archive_path(self.digest_b),
                },
            ],
            "profiles": [
                {
                    "id": "linux-example",
                    "dependencies": ["object-a", "object-b"],
                }
            ],
        }
        self.capsule_path = self.fixture / "capsule.json"
        self._write_capsule()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_capsule(self) -> None:
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def test_ingests_profile_without_private_paths_in_report(self) -> None:
        result = ingest_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
            sources={
                "object-a": self.source_a,
                "object-b": self.source_b,
            },
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.ingested_count, 2)
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(str(self.source_a), serialized)
        self.assertNotIn(str(self.source_b), serialized)

    def test_second_ingest_is_idempotent(self) -> None:
        ingest_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
            sources={
                "object-a": self.source_a,
                "object-b": self.source_b,
            },
        )
        second = ingest_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
            sources={},
        )

        self.assertEqual(second.ingested_count, 0)
        self.assertEqual(second.already_present_count, 2)
        self.assertTrue(second.complete)

    def test_missing_source_is_detected_before_copy(self) -> None:
        with self.assertRaisesRegex(
            ProfileStoreError,
            "Missing --source assignment",
        ):
            ingest_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-example",
                vault_root=self.vault,
                sources={"object-a": self.source_a},
            )

        self.assertFalse((self.vault / "objects").exists())

    def test_unknown_source_assignment_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProfileStoreError,
            "does not belong to profile",
        ):
            ingest_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-example",
                vault_root=self.vault,
                sources={
                    "object-a": self.source_a,
                    "object-b": self.source_b,
                    "typo": self.source_a,
                },
            )

    def test_verify_profile_reports_missing(self) -> None:
        result = verify_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.verified_count, 0)
        self.assertEqual(
            {item.status for item in result.objects},
            {"missing"},
        )
        self.assertFalse(self.vault.exists())

    def test_verify_profile_passes_after_ingest(self) -> None:
        ingest_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
            sources={
                "object-a": self.source_a,
                "object-b": self.source_b,
            },
        )

        result = verify_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-example",
            vault_root=self.vault,
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.verified_count, 2)

    def test_parse_source_assignments(self) -> None:
        parsed = parse_source_assignments(
            [
                f"object-a={self.source_a}",
                f"object-b={self.source_b}",
            ]
        )
        self.assertEqual(parsed["object-a"], self.source_a.absolute())

    def test_duplicate_source_assignment_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProfileStoreError,
            "Duplicate source assignment",
        ):
            parse_source_assignments(
                [
                    f"object-a={self.source_a}",
                    f"object-a={self.source_a}",
                ]
            )

    @unittest.skipIf(
        os.name == "nt",
        "Symlink creation is not reliably available on Windows CI.",
    )
    def test_symlinked_vault_parent_is_rejected_before_copy(self) -> None:
        external = self.root / "external"
        external.mkdir()
        self.vault.mkdir()
        (self.vault / "objects").symlink_to(
            external,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            ProfileStoreError,
            "symbolic-link directory component",
        ):
            ingest_profile(
                capsule_path=self.capsule_path,
                profile_id="linux-example",
                vault_root=self.vault,
                sources={
                    "object-a": self.source_a,
                    "object-b": self.source_b,
                },
            )

        self.assertEqual(list(external.rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
