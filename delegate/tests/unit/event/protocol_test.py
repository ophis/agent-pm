"""delegate/event/__init__.py: the reply protocol's names."""

import unittest

from agent_pm.delegate import event as protocol

from delegate.tests.case import Case


class ProtocolTest(Case):
    def test_the_names(self):
        self.assertEqual(
            (protocol.PROGRESS, protocol.OUTCOME), ("agent-pm-progress", "agent-pm-outcome")
        )


if __name__ == "__main__":
    unittest.main()
