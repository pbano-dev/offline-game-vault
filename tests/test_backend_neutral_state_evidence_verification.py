from __future__ import annotations

import json
from pathlib import Path
import unittest

import test_backend_neutral_state_restoration as restoration_helpers
import test_bottles_adapter
import test_umu_adapter

from offline_game_vault.bottles_adapter import (
    BottlesAdapterError,
    DEPLOYMENT_RECEIPT_NAME,
    deploy_bottles_profile,
    verify_bottles_deployment,
)
from offline_game_vault.state_manager import preserve_state
from offline_game_vault.umu_adapter import (
    RECEIPT_NAME,
    UmuAdapterError,
    materialize_umu_profile,
    verify_umu_materialization,
)


def _tamper_baseline(
    *,
    root: Path,
    receipt_name: str,
) -> None:
    receipt = restoration_helpers._read_json(root / receipt_name)
    evidence = receipt.get("state_restore")

    if not isinstance(evidence, dict):
        raise AssertionError("state_restore evidence is absent")

    baseline_relative = evidence.get("baseline_receipt")
    if not isinstance(baseline_relative, str):
        raise AssertionError("baseline_receipt is absent")

    baseline = root / baseline_relative
    baseline.write_text(
        baseline.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )


class BottlesStateEvidenceVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_bottles_adapter.BottlesAdapterTests(
            "test_deploys_mutable_derivative_without_changing_source"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

    def test_verifier_rejects_tampered_baseline_receipt(self) -> None:
        fixture = self.fixture

        restoration_helpers._write_support_files(
            fixture.fixture,
            (
                "README.md",
                "GAME.md",
                "CREDITS.md",
                "PRESERVED.md",
                "host-contract.json",
            ),
        )

        state_source = fixture.root / "state-evidence-source"
        save = (
            state_source
            / "drive_c"
            / "users"
            / "steamuser"
            / "Documents"
            / "save.dat"
        )
        save.parent.mkdir(parents=True)
        save.write_bytes(b"archived-save")

        backup = fixture.root / "state-evidence-backup"
        preserve_state(
            capsule_path=fixture.capsule_path,
            state_root=state_source,
            backup=backup,
            confirm_stopped=True,
        )

        deploy_bottles_profile(
            capsule_path=fixture.capsule_path,
            profile_id="linux-bottles",
            materialization=fixture.materialization,
            bottles_path=fixture.bottles,
            bottle_name=fixture.name,
            state_backup=backup,
            require_state_backup=True,
        )

        target = fixture.bottles / fixture.name
        _tamper_baseline(
            root=target,
            receipt_name=DEPLOYMENT_RECEIPT_NAME,
        )

        with self.assertRaisesRegex(
            BottlesAdapterError,
            "state restoration evidence",
        ):
            verify_bottles_deployment(
                bottles_path=fixture.bottles,
                bottle_name=fixture.name,
            )


class UmuStateEvidenceVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_umu_adapter.UmuAdapterTests(
            "test_materialize_run_verify_and_remove"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

    def _configure_generic_state(self) -> None:
        fixture = self.fixture

        restoration_helpers._write_support_files(
            fixture.fixture,
            (
                "README.md",
                "GAME.md",
                "CREDITS.md",
                "PRESERVED.md",
                "host.json",
            ),
        )

        fixture.capsule["persistent_state"] = [
            {
                "id": "save",
                "path": "save.bin",
                "kind": "save",
                "backup": True,
                "sensitive": True,
                "required": True,
            }
        ]

        profile = fixture.capsule["profiles"][0]
        contract = profile["umu"]
        contract.pop("state_archives", None)
        contract["protected_manifests"] = [
            item
            for item in contract["protected_manifests"]
            if Path(item["source"]).name
            not in {"config.sha256", "save.sha256"}
        ]
        contract["paths"]["prefix"] = "payload/prefix"

        fixture.capsule_path.write_text(
            json.dumps(fixture.capsule),
            encoding="utf-8",
        )

    def test_verifier_rejects_tampered_baseline_receipt(self) -> None:
        fixture = self.fixture
        self._configure_generic_state()

        source = fixture.root / "state-evidence-source"
        source.mkdir()
        (source / "save.bin").write_bytes(b"accepted-save")

        backup = fixture.root / "state-evidence-backup"
        preserve_state(
            capsule_path=fixture.capsule_path,
            state_root=source,
            backup=backup,
            confirm_stopped=True,
        )

        materialize_umu_profile(
            capsule_path=fixture.capsule_path,
            profile_id="linux-umu",
            vault_root=fixture.vault,
            destination=fixture.destination,
            state_backup=backup,
            require_state_backup=True,
        )

        _tamper_baseline(
            root=fixture.destination,
            receipt_name=RECEIPT_NAME,
        )

        with self.assertRaisesRegex(
            UmuAdapterError,
            "state restoration evidence",
        ):
            verify_umu_materialization(
                destination=fixture.destination
            )


if __name__ == "__main__":
    unittest.main()
