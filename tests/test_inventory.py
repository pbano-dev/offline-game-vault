from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.inventory import (
    InventoryError,
    build_inventory,
    write_inventory_atomic,
)


def store_object(vault: Path, payload: bytes) -> Path:
    hexadecimal = hashlib.sha256(payload).hexdigest()
    path = (
        vault
        / "objects"
        / "sha256"
        / hexadecimal[:2]
        / hexadecimal[2:4]
        / hexadecimal
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_empty_inventory_is_deterministic(self) -> None:
        first = build_inventory(vault_root=self.vault)
        second = build_inventory(vault_root=self.vault)

        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.object_count, 0)
        self.assertEqual(first.total_bytes, 0)

    def test_inventory_hashes_and_sorts(self) -> None:
        first_payload = b"z-object\n"
        second_payload = b"a-object\n"
        store_object(self.vault, first_payload)
        store_object(self.vault, second_payload)

        inventory = build_inventory(vault_root=self.vault)

        self.assertEqual(inventory.object_count, 2)
        self.assertEqual(
            inventory.total_bytes,
            len(first_payload) + len(second_payload),
        )
        digests = [item.digest for item in inventory.objects]
        self.assertEqual(digests, sorted(digests))

    def test_corrupt_object_is_rejected(self) -> None:
        path = store_object(self.vault, b"original\n")
        path.write_bytes(b"corrupt\n")

        with self.assertRaisesRegex(
            InventoryError,
            "does not match its canonical path",
        ):
            build_inventory(vault_root=self.vault)

    def test_malformed_path_is_rejected(self) -> None:
        path = self.vault / "objects/sha256/not/canonical/object"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"payload")

        with self.assertRaisesRegex(
            InventoryError,
            "Malformed content-addressed path",
        ):
            build_inventory(vault_root=self.vault)

    @unittest.skipIf(
        os.name == "nt",
        "Symlink creation is not reliably available on Windows CI.",
    )
    def test_symlink_is_rejected(self) -> None:
        external = self.root / "external.bin"
        external.write_bytes(b"external")
        hexadecimal = hashlib.sha256(b"external").hexdigest()
        link = (
            self.vault
            / "objects"
            / "sha256"
            / hexadecimal[:2]
            / hexadecimal[2:4]
            / hexadecimal
        )
        link.parent.mkdir(parents=True)
        link.symlink_to(external)

        with self.assertRaisesRegex(
            InventoryError,
            "must not be a symlink",
        ):
            build_inventory(vault_root=self.vault)

    def test_atomic_output_is_byte_identical(self) -> None:
        store_object(self.vault, b"inventory object\n")
        inventory = build_inventory(vault_root=self.vault)
        output = self.vault / "VAULT_INVENTORY.json"

        write_inventory_atomic(
            inventory=inventory,
            output=output,
            vault_root=self.vault,
        )
        first = output.read_bytes()

        write_inventory_atomic(
            inventory=inventory,
            output=output,
            vault_root=self.vault,
        )
        second = output.read_bytes()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), inventory.to_dict())
        self.assertEqual(
            list(output.parent.glob(".ogv-inventory-*")),
            [],
        )

    def test_output_inside_objects_is_rejected(self) -> None:
        inventory = build_inventory(vault_root=self.vault)

        with self.assertRaisesRegex(
            InventoryError,
            "must not be inside the object store",
        ):
            write_inventory_atomic(
                inventory=inventory,
                output=self.vault / "objects/inventory.json",
                vault_root=self.vault,
            )


if __name__ == "__main__":
    unittest.main()
