"""A usage-only Adapter returns a caller-owned sentinel without selecting a client mode."""

import sys

from agent_pm.session import Usage

from session.tests.fake_adapter import FakeAdapter, Launch


class UsageAdapter(FakeAdapter):
    keys = frozenset({"label", "cwd"})
    usage_result: Usage | None = None
    queried: list[str] = []
    starts: list[str] = []

    def usage_probe(self):
        return Launch((sys.executable, "-c", "print('quota')"), cwd=self.config["cwd"])

    def usage(self, output):
        self.queried.append(self.config["label"])
        return self.usage_result

    def headless(self):
        self.starts.append("headless")
        raise AssertionError("usage started a headless client")

    def tui(self):
        self.starts.append("tui")
        raise AssertionError("usage started a TUI client")

    def in_process(self):
        self.starts.append("in_process")
        raise AssertionError("usage started an in_process client")
