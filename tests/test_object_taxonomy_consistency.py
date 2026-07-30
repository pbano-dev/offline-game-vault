from __future__ import annotations
import tempfile, unittest
from pathlib import Path
from validate_repository import RepositoryValidation

def game():
    return {
        "id": "game", "roles": ["game_payload"],
        "kind": "game-payload", "scope": "game-specific",
        "shared": False, "format": "tar.zst"
    }

def capsule(*objects):
    return {
        "objects": list(objects), "embedded_artifacts": [],
        "profiles": [{"dependencies": ["game"]}]
    }

class TaxonomyConsistencyTests(unittest.TestCase):
    def errors(self, *objects):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            validator = RepositoryValidation(root)
            validator.check_object_granularity(
                root / "fixtures/test/capsule.json",
                capsule(*objects)
            )
            return validator.errors

    def test_shared_cache_allowed(self):
        self.assertEqual(self.errors(game(), {
            "id": "cache", "roles": ["tool"], "kind": "cache",
            "scope": "shared", "shared": True, "format": "tar.zst"
        }), [])

    def test_game_specific_host_dependency_allowed(self):
        self.assertEqual(self.errors(game(), {
            "id": "host", "roles": ["tool"], "kind": "host-dependency",
            "scope": "game-specific", "shared": False, "format": "tar.zst"
        }), [])

    def test_scope_contradiction_rejected(self):
        errors = self.errors(game(), {
            "id": "runtime", "roles": ["runtime"], "kind": "runtime",
            "scope": "shared", "shared": False, "format": "tar.zst"
        })
        self.assertTrue(any("requires shared=true" in e for e in errors))

    def test_runner_role_required(self):
        errors = self.errors(game(), {
            "id": "runner", "roles": ["tool"], "kind": "runner",
            "scope": "shared", "shared": True, "format": "tar.zst"
        })
        self.assertTrue(any("requires role 'runner'" in e for e in errors))

if __name__ == "__main__":
    unittest.main()
