from __future__ import annotations
import unittest
from offline_game_vault.umu_adapter import UmuVerificationResult

class VerificationResultTests(unittest.TestCase):
    def test_hardlink_group_count_is_exposed(self):
        result = UmuVerificationResult(
            schema=0, capsule_id="capsule", profile_id="profile",
            backend="umu", destination=".", protected_file_count=1,
            symlink_count=2, hardlink_group_count=3, verified=True
        )
        self.assertEqual(result.to_dict()["hardlink_group_count"], 3)

if __name__ == "__main__":
    unittest.main()
