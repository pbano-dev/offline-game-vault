"""Portable contract tests; real Windows execution lives in tests_windows."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest

from offline_game_vault.windows_launch import (
    FILE_MANIFEST, MANIFEST, WindowsLaunchError, prepare_windows_launch,
    relative, seal_windows_files, state_mapping,
)


class WindowsPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "portable"
        self.game = self.destination / "prefix/drive_c/Games/Demo"
        self.game.mkdir(parents=True)
        (self.game / "game.exe").write_bytes((b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little")
            + b"PE\0\0" + (0x8664).to_bytes(2, "little") + b"\0" * 14
            + (240).to_bytes(2, "little") + b"\0\0" + (0x20B).to_bytes(2, "little") + b"\0" * 238))
        (self.destination / "JUGAR.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.destination / "JUGAR.sh").chmod(0o700)
        self.capsule = {
            "capsule_id": "demo", "persistent_state": [],
            "profiles": [{"id": "linux", "adapter": "wine",
                          "playable": {"paths": {"prefix": "prefix"}},
                          "launch": {"entrypoint": "prefix/drive_c/Games/Demo/game.exe",
                                     "working_directory": "prefix/drive_c/Games/Demo"}}],
        }
        self.path = self.root / "capsule.json"

    def prepare(self):
        self.path.write_text(json.dumps(self.capsule))
        before = self.path.read_bytes()
        result = prepare_windows_launch(destination=self.destination, capsule_path=self.path, profile_id="linux")
        self.assertEqual(self.path.read_bytes(), before)
        return result, json.loads((self.destination / MANIFEST).read_text())

    def state(self, path, *, kind="save", state_id="save"):
        self.capsule["persistent_state"].append({"id": state_id, "path": path, "kind": kind, "backup": True})

    def test_reuses_game_and_declares_unverified_without_touching_capsule(self):
        inode = (self.game / "game.exe").stat().st_ino
        result, doc = self.prepare()
        self.assertEqual(result["status"], "prepared-unverified")
        self.assertFalse(doc["functional_acceptance"])
        self.assertEqual(doc["game_root"], "prefix/drive_c/Games/Demo")
        self.assertEqual((self.game / "game.exe").stat().st_ino, inode)
        self.assertEqual(len(list(self.destination.rglob("*.exe"))), 1)
        self.assertIn(b"-Action Play\r\n", (self.destination / "JUGAR_WINDOWS.bat").read_bytes())
        self.assertIn('JUGAR.sh', (self.destination / "JUGAR_LINUX.sh").read_text())

    @unittest.skipIf(os.name == "nt", "Linux launcher guard")
    def test_pending_windows_recovery_blocks_linux_play_and_removal(self):
        remove = self.destination / "DESINSTALAR.sh"
        remove.write_text("#!/bin/sh\nexit 0\n")
        self.prepare()
        journal = self.destination / ".ogv-windows/active.json"
        journal.parent.mkdir()
        journal.write_text("{}")
        for name in ("JUGAR.sh", "JUGAR_LINUX.sh", "DESINSTALAR.sh"):
            result = subprocess.run(["sh", str(self.destination / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Recover", result.stderr)
        journal.unlink()
        self.assertEqual(subprocess.run(["sh", str(remove)]).returncode, 0)

    def test_invalid_pe_offset_is_refused(self):
        exe = self.game / "game.exe"
        data = bytearray(exe.read_bytes())
        data[60:64] = (0xffffffff).to_bytes(4, "little")
        exe.write_bytes(data)
        self.assertEqual(self.prepare()[0]["status"], "blocked")

    def test_empty_seed_is_managed_so_host_save_cannot_be_used(self):
        self.state("drive_c/users/steamuser/AppData/Roaming/Demo/save")
        _, doc = self.prepare()
        self.assertEqual(doc["state"][0]["folder"], "RoamingAppData")
        self.assertFalse(doc["state"][0]["initial_present"])

    def test_game_local_identity_and_gse_account(self):
        self.state("drive_c/users/steamuser/AppData/Roaming/EldenRing/76561198000000000")
        target = "drive_c/users/steamuser/AppData/Roaming/GSE Saves/settings/configs.user.ini"
        self.state(target, kind="identity", state_id="gse")
        file = self.destination / "prefix" / target
        file.parent.mkdir(parents=True)
        file.write_text("[user::general]\naccount_steamid=76561198000000000\nlanguage=spanish\n")
        self.state("drive_c/Games/Demo/settings.ini", kind="configuration", state_id="settings")
        _, doc = self.prepare()
        self.assertEqual(doc["status"], "prepared-unverified")
        self.assertEqual(doc["state"][2]["folder"], "Game")

    def test_missing_declared_gse_identity_does_not_silently_select_another_account(self):
        self.state("drive_c/users/steamuser/AppData/Roaming/EldenRing/76561198000000000")
        self.state("drive_c/users/steamuser/AppData/Roaming/GSE Saves/settings/configs.user.ini", kind="identity", state_id="gse")
        result, _ = self.prepare()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("identity is missing", result["issues"][0])

    def test_mismatching_identity_blocks_only_windows(self):
        self.state("drive_c/users/steamuser/AppData/Roaming/EldenRing/76561198000000000")
        target = "drive_c/users/steamuser/AppData/Roaming/GSE Saves/settings/configs.user.ini"
        self.state(target, kind="identity", state_id="gse")
        file = self.destination / "prefix" / target
        file.parent.mkdir(parents=True)
        file.write_text("[user::general]\naccount_steamid=76561198000000001\n")
        result, _ = self.prepare()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("identity differs", result["issues"][0])
        self.assertTrue((self.destination / "JUGAR.sh").exists())

    def test_unsafe_or_ambiguous_paths(self):
        for path in ("../escape", "C:/game", "a//b", "a/./b", "a\\b", "NUL.txt", "x/COM¹", "a.", "a ", "/root", "a:b"):
            with self.subTest(path=path), self.assertRaises(WindowsLaunchError):
                relative(path)
        for path in ("drive_c/users/user/AppData/Roaming", "drive_c/unmapped/save"):
            with self.assertRaises(WindowsLaunchError):
                state_mapping({"path": path})

    def test_case_collision_blocks_preparation(self):
        (self.game / "DATA").write_bytes(b"a")
        (self.game / "data").write_bytes(b"b")
        if len(list(self.game.iterdir())) != 3:
            self.skipTest("Filesystem is case insensitive")
        result, _ = self.prepare()
        self.assertEqual(result["status"], "blocked")

    def test_symlink_in_game_blocks_preparation(self):
        try:
            (self.game / "linked").symlink_to("game.exe")
        except OSError:
            self.skipTest("Symlinks unavailable")
        self.assertEqual(self.prepare()[0]["status"], "blocked")

    def test_overlap_is_rejected(self):
        self.state("drive_c/users/user/AppData/Roaming/Demo")
        self.state("drive_c/users/user/AppData/Roaming/Demo/save", state_id="nested")
        self.assertEqual(self.prepare()[0]["status"], "blocked")

    def test_integrity_manifest_reuses_hashes_and_excludes_mutable_state(self):
        self.state("drive_c/Games/Demo/settings.ini", kind="configuration")
        (self.game / "settings.ini").write_bytes(b"mutable")
        self.prepare()
        records = [{"path": file.relative_to(self.destination).as_posix(),
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "bytes": file.stat().st_size}
                   for file in self.game.iterdir()]
        additions = seal_windows_files(self.destination, records)
        document = json.loads((self.destination / FILE_MANIFEST).read_text())
        self.assertEqual(len(document["files"]), 1)
        self.assertTrue(document["files"][0]["path"].endswith("game.exe"))
        self.assertEqual(len(additions), 2)

    def test_keeps_steam_app_identity_but_omits_linux_runtime_environment(self):
        self.capsule["profiles"][0]["launch"]["environment"] = {
            "SteamAppId": "123", "SteamGameId": "123", "LANGUAGE": "spanish",
            "STEAM_COMPAT_DATA_PATH": "/linux/prefix", "WINEPREFIX": "/linux/prefix",
        }
        _, doc = self.prepare()
        self.assertEqual(doc["status"], "prepared-unverified")
        self.assertEqual(doc["environment"], {"SteamAppId": "123", "SteamGameId": "123", "LANGUAGE": "spanish"})

    def test_unknown_native_umu_layout_is_blocked(self):
        self.capsule["profiles"][0]["adapter"] = "umu"
        self.assertEqual(self.prepare()[0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
