import functools
import io
import json
import os
import tempfile
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import config, router, runs
from agent_pm.session import SessionStatus
from orchestrator_test_helpers.unit import run_fixtures
from orchestrator_test_helpers.unit.clock import logs_at
from orchestrator_test_helpers.unit.fake_api import FakeApi
from orchestrator_test_helpers.unit.router.data import (
    AGENT,
    CONFIG,
    NO,
    NOW,
    RUNTIMES,
    STATES,
    WEST,
    ago,
)
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.run_fixtures import HARNESS, logged, show


class Base(run_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.write_local(f'work_dir = "{self.work}"\n')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_path = os.path.join(self.tmp, "config.toml")
        self.write_config(CONFIG)
        self.lines, self.hist, self.names = [], {}, {}
        self.logs, self.mark, self.raw, self.taken = None, 0, "", None
        self.api = FakeApi()

    def write_config(self, text):
        self.write(self.config_path, text)

    def sid(self, name):
        s = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
        self.names[s] = name
        return s

    def unmap(self, text):
        for s, name in self.names.items():
            text = text.replace(s, name)
        return text

    def fake(self, *issues):
        return FakeLinear(issues, self.hist)

    def moved(self, ident, minutes_ago, state=STATES["In Progress"], actor=AGENT):
        self.hist.setdefault(ident, []).insert(
            0, {"createdAt": ago(minutes=minutes_ago), "actorId": actor, "toStateId": state}
        )

    def resumable(self, ident, sid, minutes_ago, role="researcher"):
        self.moved(ident, minutes_ago + 1)
        self.add("start", ident, sid, minutes_ago, role)
        self.touch(sid, 40)

    def row(self, kind, ident, sid, role="researcher", runtime=None, **ago):
        return json.dumps(
            {
                "ts": (NOW - timedelta(**ago)).astimezone(WEST).isoformat(timespec="seconds"),
                "kind": kind,
                "issue": ident,
                "sid": self.sid(sid),
                "role": role,
                "runtime": runtime or RUNTIMES.get(role, "a"),
            }
        )

    def add(self, kind, ident, sid, minutes_ago, role="researcher", runtime=None):
        self.lines.append(self.row(kind, ident, sid, role, runtime, minutes=minutes_ago))

    def limit(self, runtime, resets_at, ident="TASK-9"):
        self.lines.append(
            json.dumps(
                {
                    "ts": NOW.astimezone(WEST).isoformat(timespec="seconds"),
                    "kind": "limit",
                    "issue": ident,
                    "sid": self.sid("limited"),
                    "runtime": runtime,
                    "resets_at": resets_at,
                }
            )
        )

    def touch(self, sid, minutes_ago):
        # the session's transcript, last written minutes_ago
        at = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        self.api.statuses[self.sid(sid)] = SessionStatus("yes", at, f"resume {sid}")

    def write_runs(self):
        self.write(self.runs, "".join(f"{line}\n" for line in self.lines))
        for e in runs.parse_log(self.runs):
            self.api.statuses.setdefault(e.sid, NO)

    def log_dir(self, again):
        if not again or self.logs is None:
            self.logs = os.path.join(self.work, "logs")
            path = os.path.join(self.logs, "orchestrator.jsonl")
            if os.path.exists(path):
                os.unlink(path)
        self.mark = len(logged(self.logs))
        return logs_at(self.logs)

    def board(self, fake, use, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again):
            out = use(
                router.Board(
                    functools.partial(fake, service=HARNESS),
                    runs.parse_log(self.runs),
                    NOW,
                    dry,
                    cfg,
                    router.Gate(self.runs, NOW, dry, driver=self.api) if gate else None,
                )
            )
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, dry=False, again=False):
        def use(b):
            if cands := b.recover():
                return ("resume", *cands[0])
            return ("new",) if b.queue() else None

        run = self.board(fake, use, dry, again=again)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(
                f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}"
            )
        return "new" if run else ""

    def claim(self, fake, dry=False, full=(), recover=False, gate=False, again=False):
        def use(b):
            recover and b.recover()
            return b.take(full=full)

        self.taken = taken = self.board(fake, use, dry, gate, again)
        return (
            taken
            and f"{taken[0]['identifier']} {taken[0]['url']} {taken[0]['project']['name']}"
            or ""
        )

    def said(self):
        events = logged(self.logs)[self.mark :] + [
            json.loads(x) for x in self.raw.splitlines() if x.startswith("{")
        ]
        self.assertEqual({e["src"] for e in events} - {"router"}, set())
        return [self.unmap(show(e)) for e in events]
