from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "schemas" / "capsule.schema.json").read_text(
        encoding="utf-8"
    )
)

VALID_KINDS = {
    "game-payload",
    "runner",
    "runtime",
    "backend",
    "tool",
    "cache",
    "host-dependency",
    "documentation",
    "state-seed",
    "historical",
    "other",
}

VALID_SCOPES = {
    "shared",
    "game-specific",
    "historical",
}


def capsule_fixture() -> dict[str, object]:
    return {
        "schema": 0,
        "capsule_id": "taxonomy-schema-test",
        "game": {
            "title": "Taxonomy Schema Test",
            "source_store": "test",
            "preserved_version": "1",
        },
        "documents": {
            "readme": "README.md",
            "game_sheet": "GAME.md",
            "credits": "CREDITS.md",
            "preserved_by": "PRESERVED_BY.md",
        },
        "objects": [
            {
                "id": "game-object",
                "digest": "sha256:" + ("0" * 64),
                "roles": ["game_payload"],
                "format": "tar.zst",
                "required": True,
                "shared": False,
                "kind": "game-payload",
                "scope": "game-specific",
            }
        ],
        "profiles": [
            {
                "id": "test-profile",
                "platform": "linux",
                "adapter": "other",
                "status": "not_tested",
                "dependencies": ["game-object"],
                "host_contract": "host-contract.json",
                "launch": {
                    "entrypoint": "launcher.sh"
                },
            }
        ],
    }


class ObjectTaxonomySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(SCHEMA)

    def assert_valid(
        self,
        capsule: dict[str, object],
    ) -> None:
        errors = list(
            self.validator.iter_errors(capsule)
        )
        self.assertEqual(
            errors,
            [],
            [error.message for error in errors],
        )

    def test_all_documented_kinds_are_valid(self) -> None:
        for kind in sorted(VALID_KINDS):
            with self.subTest(kind=kind):
                capsule = capsule_fixture()
                capsule["objects"][0]["kind"] = kind
                self.assert_valid(capsule)

    def test_all_documented_scopes_are_valid(self) -> None:
        for scope in sorted(VALID_SCOPES):
            with self.subTest(scope=scope):
                capsule = capsule_fixture()
                capsule["objects"][0]["scope"] = scope
                self.assert_valid(capsule)

    def test_unknown_kind_is_rejected(self) -> None:
        capsule = copy.deepcopy(
            capsule_fixture()
        )
        capsule["objects"][0]["kind"] = "game_payload"
        errors = list(
            self.validator.iter_errors(capsule)
        )
        self.assertTrue(errors)

    def test_unknown_scope_is_rejected(self) -> None:
        capsule = copy.deepcopy(
            capsule_fixture()
        )
        capsule["objects"][0]["scope"] = "global"
        errors = list(
            self.validator.iter_errors(capsule)
        )
        self.assertTrue(errors)


    def test_experimental_variant_accepts_shared_runtime_id(self) -> None:
        capsule = copy.deepcopy(capsule_fixture())
        capsule["profiles"][0]["variant"] = {
            "kind": "experimental",
            "source_profile_id": "source-profile",
            "backend": "umu",
            "runner_id": "preserved-proton",
            "shared_runtime_id": "umu-runtime-example",
            "acceptance_inherited": False,
        }
        self.assert_valid(capsule)

    def test_experimental_variant_keeps_legacy_backend_template_compatible(self) -> None:
        capsule = copy.deepcopy(capsule_fixture())
        capsule["profiles"][0]["variant"] = {
            "kind": "experimental",
            "source_profile_id": "source-profile",
            "backend": "umu",
            "runner_id": "preserved-proton",
            "backend_template": "legacy-profile-id",
            "acceptance_inherited": False,
        }
        self.assert_valid(capsule)


if __name__ == "__main__":
    unittest.main()
