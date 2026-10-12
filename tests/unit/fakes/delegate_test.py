"""tests/fakes/delegate.py: FakeDelegate passes DelegateContract."""

import unittest

from tests.case import Case
from tests.contracts.delegate import DelegateContract
from tests.fakes.delegate import FakeDelegate


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
