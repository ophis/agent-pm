import json
import os
import sys
import tempfile
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.case import Case
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.streams import Stderr


class Logs(Case):
    TS = "2026-10-09T00:26:33-04:00"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.logs = os.path.join(self.tmp, "logs")
        self.path = os.path.join(self.logs, "orchestrator.jsonl")
        linear.use_logs(self.logs)

    def stderr(self, call, fail=None, *, tty=False):
        err = Stderr(tty=tty, fail=fail)
        with (
            mock.patch.object(sys, "stderr", err),
            frozen(self.TS),
        ):
            out = call()
        return out, err.getvalue()

    def text(self, path=None):
        try:
            with open(path or self.path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self):
        return [json.loads(line) for line in self.text().splitlines()]

    def json(self, src, kind, issue=None, **fields):
        line = {
            "ts": self.TS,
            "src": src,
            "kind": kind,
            **({"issue": issue} if issue else {}),
            **fields,
        }
        return json.dumps(line, ensure_ascii=False) + "\n"
