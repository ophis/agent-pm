"""Utils public exports."""

import unittest

from agent_pm import utils

from utils.tests.case import Case


class Exports(Case):
    def test_all_is_sorted_without_duplicates(self):
        self.assertEqual(utils.__all__, sorted(set(utils.__all__)))


if __name__ == "__main__":
    unittest.main()
