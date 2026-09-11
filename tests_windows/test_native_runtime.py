"""Execute the shipped runtime with a real PE probe on native Windows.

All host mutations are confined to an unpredictable, test-owned AppData folder.
No commercial game, Wine prefix registry, or existing user's save is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from offline_game_vault.windows_launch import MANIFEST, RUNTIME, prepare_windows_launch, seal_windows_files

PROBE = r'''
using System;
using System.IO;
using System.Diagnostics;
using System.Threading;
public static class Probe {
    public static int Main(string[] args) {
        string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), Environment.GetEnvironmentVariable("OGV_TEST_SUBDIR"));
        Directory.CreateDirectory(directory);
        string file = Path.Combine(directory, "save.txt");
        string mode = Environment.GetEnvironmentVariable("OGV_TEST_MODE") ?? "normal";
        if (mode == "child") {
            Environment.SetEnvironmentVariable("OGV_TEST_MODE", "normal");
            var child = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName);
            child.UseShellExecute = false;
            Process.Start(child);
            return 0;
        }
        Thread.Sleep(300);
        string initial = File.Exists(file) ? File.ReadAllText(file) : "new";
        File.WriteAllText(file, initial + "+progress");
        File.WriteAllLines(Path.Combine(directory, "args.txt"), args);
        File.WriteAllText(Path.Combine(directory, "cwd.txt"), Environment.CurrentDirectory);
        File.WriteAllText(Path.Combine(directory, "identity.txt"), Environment.GetEnvironmentVariable("OGV_TEST_IDENTITY") ?? "");
        string iniRelative = Environment.GetEnvironmentVariable("OGV_TEST_INI");
        if (!String.IsNullOrEmpty(iniRelative)) {
            string ini = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), iniRelative);
            File.WriteAllText(Path.Combine(directory, "actual-ini.txt"), File.ReadAllText(ini));
        }
        if (mode == "hold") {
            File.WriteAllText(Path.Combine(Environment.CurrentDirectory, "ready.txt"), Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(60000);
        }
        return mode == "fail" ? 23 : 0;
    }
}
'''


@unittest.skipUnless(os.name == "nt", "Requires native Windows")
class NativeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compilation = tempfile.TemporaryDirectory(prefix="ogv-pe-")
        cls.addClassCleanup(cls.compilation.cleanup)
        root = Path(cls.compilation.name)
        cls.probe = root / "probe.exe"
        cls.powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        source = root / "probe.cs"
        source.write_text(PROBE, encoding="utf-8")
        builder = root / "build.ps1"
        builder.write_text("param($Source, $Destination)\n$ErrorActionPreference = 'Stop'\nAdd-Type -Path $Source -OutputAssembly $Destination -OutputType ConsoleApplication\n")
        result = subprocess.run([cls.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(builder), str(source), str(cls.probe)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="OGV space & unicode-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "portable"
        self.game = self.destination / "prefix/drive_c/Games/Demo"
        self.game.mkdir(parents=True)
        self.exe = self.game / ("probe-" + uuid.uuid4().hex + ".exe")
        shutil.copyfile(self.probe, self.exe)
        self.relative = "OGV-Native-Test-" + uuid.uuid4().hex
        self.host = Path(os.environ["APPDATA"]) / self.relative
        self.addCleanup(lambda: shutil.rmtree(self.host, ignore_errors=True))
        self.source = self.destination / "prefix/drive_c/users/steamuser/AppData/Roaming" / self.relative
        self.source.mkdir(parents=True)
        (self.source / "save.txt").write_text("preserved", encoding="utf-8")
        self.args = ["", "space value", 'a"b', "trailing\\", "& no shell | ;", "espa\u00f1ol"]
        self.profile = {
            "id": "linux", "adapter": "wine", "playable": {"paths": {"prefix": "prefix"}},
            "launch": {"entrypoint": self.exe.relative_to(self.destination).as_posix(),
                       "working_directory": self.game.relative_to(self.destination).as_posix(),
                       "arguments": self.args,
                       "environment": {"OGV_TEST_SUBDIR": self.relative, "OGV_TEST_IDENTITY": "account-and-spanish"}},
        }
        self.capsule = {"capsule_id": "native-probe", "profiles": [self.profile],
                        "persistent_state": [{"id": "save", "kind": "save", "backup": True,
                                              "path": "drive_c/users/steamuser/AppData/Roaming/" + self.relative}]}

    def prepare(self):
        path = self.root / "capsule.json"
        path.write_text(json.dumps(self.capsule), encoding="utf-8")
        result = prepare_windows_launch(destination=self.destination, capsule_path=path, profile_id="linux")
        self.assertEqual(result["status"], "prepared-unverified", result)
        seal_windows_files(self.destination, [{"path": self.exe.relative_to(self.destination).as_posix(),
                            "sha256": hashlib.sha256(self.exe.read_bytes()).hexdigest(), "bytes": self.exe.stat().st_size}])

    def command(self, action):
        return [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.destination / RUNTIME), "-Action", action]

    def run_action(self, action, expected=0):
        result = subprocess.run(self.command(action), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def host_seed(self):
        self.host.mkdir()
        (self.host / "save.txt").write_text("host-original", encoding="utf-8")
        (self.host / "extra.dat").write_bytes(b"existing-host-only-data")

    def assert_host_restored(self):
        self.assertEqual((self.host / "save.txt").read_text(), "host-original")
        self.assertEqual((self.host / "extra.dat").read_bytes(), b"existing-host-only-data")
        self.assertEqual(sorted(p.name for p in self.host.iterdir()), ["extra.dat", "save.txt"])
        self.assertFalse(list(self.host.parent.glob(self.relative + ".ogv-*")))
        self.assertFalse((self.destination / ".ogv-windows/active.json").exists())

    def test_real_pe_roundtrip_keeps_host_and_newest_progress_arguments_and_directory(self):
        self.host_seed()
        self.prepare()
        self.run_action("Verify")
        self.run_action("Play")
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress")
        self.assertEqual((self.source / "args.txt").read_text(encoding="utf-8-sig").splitlines(), self.args)
        self.assertEqual(Path((self.source / "cwd.txt").read_text()), self.game)
        self.assertEqual((self.source / "identity.txt").read_text(), "account-and-spanish")
        self.run_action("Play")
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress+progress")
        self.assertFalse(list(self.source.parent.glob(self.relative + ".ogv-*")))
        self.assertFalse(list((self.destination / ".ogv-windows").glob("*/after")))

    def test_identity_ini_is_installed_byte_for_byte_and_original_host_ini_restored(self):
        identity_relative = self.relative + "-GSE/settings/configs.user.ini"
        host_root = Path(os.environ["APPDATA"]) / (self.relative + "-GSE")
        host_ini = host_root / "settings/configs.user.ini"
        host_ini.parent.mkdir(parents=True)
        host_ini.write_bytes(b"host-identity")
        self.addCleanup(lambda: shutil.rmtree(host_root, ignore_errors=True))
        logical = "drive_c/users/steamuser/AppData/Roaming/" + identity_relative
        source_ini = self.destination / "prefix" / logical
        source_ini.parent.mkdir(parents=True)
        content = b"[user::general]\r\naccount_steamid=76561198000000000\r\nlanguage=spanish\r\n"
        source_ini.write_bytes(content)
        self.capsule["persistent_state"].append({"id": "identity", "kind": "identity", "backup": True, "path": logical})
        self.profile["launch"]["environment"]["OGV_TEST_INI"] = identity_relative
        self.prepare()
        self.run_action("Play")
        self.assertEqual((self.source / "actual-ini.txt").read_bytes(), content)
        self.assertEqual(source_ini.read_bytes(), content)
        self.assertEqual(host_ini.read_bytes(), b"host-identity")
        self.assertEqual(sorted(p.name for p in host_ini.parent.iterdir()), ["configs.user.ini"])

    def test_fresh_seed_never_uses_existing_host_save(self):
        self.host_seed()
        shutil.rmtree(self.source)
        self.prepare()
        self.run_action("Play")
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "new+progress")

    def test_absent_host_is_restored_to_absence(self):
        self.prepare()
        self.run_action("Play")
        self.assertFalse(self.host.exists())
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress")

    def test_game_nonzero_exit_still_preserves_progress_and_restores_host(self):
        self.host_seed()
        self.profile["launch"]["environment"]["OGV_TEST_MODE"] = "fail"
        self.prepare()
        self.run_action("Play", expected=23)
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress")

    def test_waits_for_descendant_after_game_launcher_exits(self):
        self.host_seed()
        self.profile["launch"]["environment"]["OGV_TEST_MODE"] = "child"
        self.prepare()
        self.run_action("Play")
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress")

    def test_corrupt_executable_fails_before_host_mutation(self):
        self.host_seed()
        self.prepare()
        self.exe.write_bytes(b"corrupted")
        self.run_action("Play", expected=1)
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved")

    def test_corrupt_contract_is_refused(self):
        self.host_seed()
        self.prepare()
        with (self.destination / MANIFEST).open("ab") as stream:
            stream.write(b" ")
        self.run_action("Play", expected=1)
        self.assert_host_restored()

    def test_recovery_after_launcher_kill_retains_progress_and_restores_host(self):
        self.host_seed()
        self.profile["launch"]["environment"]["OGV_TEST_MODE"] = "hold"
        self.prepare()
        process = subprocess.Popen(self.command("Play"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 30
            while not (self.game / "ready.txt").exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    out, err = process.communicate()
                    self.fail((out + err).decode(errors="replace"))
                time.sleep(.1)
            self.assertTrue((self.game / "ready.txt").exists(), "Probe never launched")
        finally:
            process.kill()
            process.communicate(timeout=10)
        # Job destruction is asynchronous; verify the child is actually gone.
        pid = int((self.game / "ready.txt").read_text())
        check = subprocess.run([self.powershell, "-NoProfile", "-Command", f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ $p.WaitForExit(5000) | Out-Null; if (!$p.HasExited) {{ exit 1 }} }}"], timeout=10)
        self.assertEqual(check.returncode, 0, "Job did not terminate its child")
        self.run_action("Play", expected=1)
        journal_path = self.destination / ".ogv-windows/active.json"
        original_journal = journal_path.read_bytes()
        journal = json.loads(original_journal)
        journal["machine"] = "different-host"
        journal_path.write_text(json.dumps(journal))
        self.run_action("Recover", expected=1)
        self.assertEqual((self.host / "save.txt").read_text(), "preserved+progress")
        journal_path.write_bytes(original_journal)
        # Recovery must work even if the executable has become unavailable.
        self.exe.write_bytes(b"damaged-after-crash")
        self.run_action("Recover")
        self.assert_host_restored()
        self.assertEqual((self.source / "save.txt").read_text(), "preserved+progress")
        self.run_action("Recover")
        self.assert_host_restored()

    def test_reparse_point_host_target_refused_before_mutation(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "save.txt").write_text("untouched")
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(self.host), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: self.host.rmdir() if self.host.is_junction() else None)
        self.prepare()
        self.run_action("Play", expected=1)
        self.assertEqual((outside / "save.txt").read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
