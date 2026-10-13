import os
import time
from unittest import mock

from orchestrator_test_helpers.unit.writeback.base_fixtures import Base


class UsageLimit(Base):
    def setUp(self):
        super().setUp()
        # POSIX TZ: CST-8 is UTC+8
        tz = mock.patch.dict(os.environ, {"TZ": "CST-8"})
        tz.start()
        self.addCleanup(time.tzset)
        self.addCleanup(tz.stop)
        time.tzset()
