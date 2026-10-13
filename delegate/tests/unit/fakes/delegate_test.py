"""delegate/tests/fakes/delegate.py: FakeDelegate passes DelegateContract."""

import unittest

from delegate.tests.contracts.delegate import DelegateContract
from delegate.tests.fakes.delegate import FakeDelegate
from tests.case import Case


class FakeDelegateContract(DelegateContract, Case):
    delegate_cls = FakeDelegate
    ROLE = "dummy"
    DISPLAYABLE = False

    def make(self, workdir, **kw):
        return FakeDelegate(
            **{"role": self.ROLE, "task": None, "input": "hello", "workdir": workdir, **kw}
        )

    def end(self, record):
        FakeDelegate.end(record.workdir)

    def finish(self, record):
        FakeDelegate.finish(record.workdir)


if __name__ == "__main__":
    unittest.main()
