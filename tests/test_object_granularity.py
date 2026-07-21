from __future__ import annotations

import json
import unittest
from pathlib import Path


class FixtureObjectGranularityTests(unittest.TestCase):
    def test_all_public_fixtures_use_one_game_object(self) -> None:
        root = Path(__file__).resolve().parents[1]
        capsules = sorted((root / "fixtures").glob("*/capsule.json"))
        self.assertTrue(capsules)

        for path in capsules:
            with self.subTest(capsule=path.parent.name):
                capsule = json.loads(path.read_text(encoding="utf-8"))
                objects = capsule.get("objects", [])
                game_objects = [
                    item for item in objects
                    if isinstance(item, dict)
                    and "game_payload" in item.get("roles", [])
                ]
                self.assertEqual(len(game_objects), 1)
                game = game_objects[0]
                self.assertIsNot(game.get("shared"), True)
                self.assertNotEqual(game.get("format"), "file")
                game_id = game["id"]
                object_ids = {item["id"] for item in objects}

                for item in objects:
                    if item is game:
                        continue
                    self.assertIs(item.get("shared"), True)
                    roles = set(item.get("roles", []))
                    self.assertTrue(roles)
                    self.assertLessEqual(roles, {"runner", "runtime"})
                    self.assertNotEqual(item.get("format"), "file")

                seen: set[str] = set()
                for item in capsule.get("embedded_artifacts", []):
                    self.assertNotIn(item["id"], object_ids)
                    self.assertNotIn(item["id"], seen)
                    seen.add(item["id"])
                    self.assertEqual(item["container_object"], game_id)

                for profile in capsule.get("profiles", []):
                    self.assertIn(game_id, profile.get("dependencies", []))


if __name__ == "__main__":
    unittest.main()
