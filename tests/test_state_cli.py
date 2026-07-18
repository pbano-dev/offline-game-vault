from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from offline_game_vault.cli import main


class StateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        self.state_root = self.root / "state"
        self.fixture.mkdir()
        self.state_root.mkdir()

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

        self.capsule = self.fixture / "capsule.json"
        self.capsule.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "state-cli-test",
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
                                "objects/sha256/aa/aa/"
                                + "a" * 64
                            ),
                        }
                    ],
                    "persistent_state": [
                        {
                            "id": "save",
                            "path": "save/data.bin",
                            "kind": "save",
                            "backup": True,
                            "sensitive": True,
                            "required": True,
                        }
                    ],
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
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        save = self.state_root / "save" / "data.bin"
        save.parent.mkdir()
        save.write_bytes(b"preserved")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_audit_preserve_verify_and_restore_json(self) -> None:
        code, stdout, stderr = self._run(
            [
                "audit-capsule",
                "--capsule",
                str(self.capsule),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        audit = json.loads(stdout)
        self.assertTrue(audit["valid"])
        self.assertTrue(audit["operational"])
        self.assertNotIn(str(self.root), stdout)

        backup = self.root / "backup"
        code, stdout, stderr = self._run(
            [
                "preserve-state",
                "--capsule",
                str(self.capsule),
                "--state-root",
                str(self.state_root),
                "--backup",
                str(backup),
                "--confirm-stopped",
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        preserved = json.loads(stdout)
        self.assertTrue(preserved["complete"])
        self.assertNotIn(str(self.root), stdout)

        code, stdout, stderr = self._run(
            [
                "verify-state-backup",
                "--capsule",
                str(self.capsule),
                "--backup",
                str(backup),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["verified"])
        self.assertNotIn(str(self.root), stdout)

        (self.state_root / "save" / "data.bin").write_bytes(
            b"changed"
        )
        snapshot = self.root / "snapshot"
        code, stdout, stderr = self._run(
            [
                "restore-state",
                "--capsule",
                str(self.capsule),
                "--state-root",
                str(self.state_root),
                "--backup",
                str(backup),
                "--snapshot",
                str(snapshot),
                "--confirm-stopped",
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        restored = json.loads(stdout)
        self.assertTrue(restored["complete"])
        self.assertNotIn(str(self.root), stdout)
        self.assertEqual(
            (self.state_root / "save" / "data.bin").read_bytes(),
            b"preserved",
        )

    def test_preserve_requires_stopped_confirmation(self) -> None:
        code, stdout, stderr = self._run(
            [
                "preserve-state",
                "--capsule",
                str(self.capsule),
                "--state-root",
                str(self.state_root),
                "--backup",
                str(self.root / "backup"),
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("confirm-stopped", stderr)
        self.assertNotIn(str(self.root), stderr)


if __name__ == "__main__":
    unittest.main()
