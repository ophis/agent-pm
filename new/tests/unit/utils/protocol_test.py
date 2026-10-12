"""utils/protocol.py: the reply protocol's names, the objects composer exports."""

import unittest

from agent_pm import composer
from agent_pm.utils import protocol

from new.tests.case import Case


class ProtocolTest(Case):
    def test_the_names(self):
        self.assertEqual(
            (protocol.PROGRESS, protocol.OUTCOME), ("agent-pm-progress", "agent-pm-outcome")
        )

    def test_composer_exports_these_objects(self):
        self.assertIs(composer.PROGRESS, protocol.PROGRESS)
        self.assertIs(composer.OUTCOME, protocol.OUTCOME)


if __name__ == "__main__":
    unittest.main()
