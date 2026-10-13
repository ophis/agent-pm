import functools
import io
import json
import os
import tempfile
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, router, runs
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.engine.usage import types as runtime_usage
from agent_pm.session import SessionStatus
from orchestrator_tests.case import PYTHONPATH
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.router.data import (
    AGENT,
    CONFIG,
    ENG_RUN,
    HUMANS,
    NO,
    NOW,
    OUTER,
    RUNTIMES,
    STATES,
    TASK_LABELS,
    WEST,
    ago,
    outer_args,
)
from orchestrator_tests.unit.router.fakes import FakeLinear
from orchestrator_tests.unit.run import support as run_fixtures
from orchestrator_tests.unit.run.support import (
    ENGINEER,
    HARNESS,
    ID,
    PROJECT,
    SID,
    USER_NOTE,
    Gql,
    logged,
    node,
    show,
)
from orchestrator_tests.unit.streams import Stderr
from orchestrator_tests.unit.target.support import FakeRun


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

    def log_dir(self, *, again):
        if not again or self.logs is None:
            self.logs = os.path.join(self.work, "logs")
            path = os.path.join(self.logs, "orchestrator.jsonl")
            if os.path.exists(path):
                os.unlink(path)
        self.mark = len(logged(self.logs))
        return logs_at(self.logs)

    def board(self, fake, use, *, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=again):
            out = use(
                routing.Board(
                    functools.partial(fake, service=HARNESS),
                    runs.parse_log(self.runs),
                    cfg,
                    routing.BoardPolicy(
                        now=NOW,
                        dry=dry,
                        gate=runtime_usage.Gate(self.runs, NOW, dry=dry, driver=self.api)
                        if gate
                        else None,
                    ),
                )
            )
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, *, dry=False, again=False):
        def use(b):
            if cands := b.recover():
                return ("resume", *cands[0])
            return ("new",) if b.queue() else None

        run = self.board(fake, use, dry=dry, again=again)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(
                f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}"
            )
        return "new" if run else ""

    def claim(self, fake, full=(), *, dry=False, recover=False, gate=False, again=False):
        def use(b):
            recover and b.recover()
            return b.take(full=full)

        self.taken = taken = self.board(fake, use, dry=dry, gate=gate, again=again)
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


class Start:
    """The injected start: records each launch Namespace; per issue an exit code (`exits`) or an
    exception to raise.
    """

    def __init__(self, exits=None, raises=None):
        self.launches, self.exits, self.raises = [], exits or {}, raises or {}

    def __call__(self, a):
        self.launches.append(a)
        if a.issue in self.raises:
            raise self.raises[a.issue]
        return self.exits.get(a.issue, 0)


class TickBase(Base):
    def tick(
        self, fake, *argv, hour=2, live=(), start=None, again=False, now=NOW, tty=False, outer=None
    ):
        # outer: main's run and keychain; main then starts through its own outer
        self.api.live = None if live is None else list(live)
        self.api.order, self.api.usage_calls, self.api.info = [], [], []
        self.start = None if outer else start or Start()
        self.write_runs()
        err = Stderr(tty=tty)
        with (
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.log_dir(again=again),
        ):
            rc = router.main(
                list(argv),
                now=now,
                config_path=self.config_path,
                hour=hour,
                services=router.RouterServices(
                    gql=fake, driver=self.api, start=self.start, **outer or {}
                ),
            )
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        with open(self.runs) as f:
            self.state = self.unmap(f.read())
        return rc

    def before(self):
        return self.unmap("".join(f"{line}\n" for line in self.lines))

    def last_run(self):
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), ["ts", "kind", "issue", "sid", "role", "runtime"])
        return entry["kind"], entry["issue"], entry["sid"], entry["role"], entry["runtime"]

    def launched(self):
        (launch,) = self.start.launches
        return launch.issue

    def launched_runs(self):
        return [(a.issue, a.mode) for a in self.start.launches]


class OuterBase(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(OUTER)
        self.gql = Gql(issue=node(comments=[USER_NOTE]))
        self.run_ = FakeRun(ENG_RUN)
        self.missing, self.keychain_calls = set(), []

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def outer(self, assignee=ENGINEER, task=None, mode="new", ident=ID, sid=SID, runtime="a"):
        a = outer_args(ident, PROJECT, assignee, sid, task, mode, runtime)
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=True):
            rc = router.outer(
                a,
                cfg,
                gql=functools.partial(self.gql, service=HARNESS),
                run=self.run_,
                keychain=self.keychain,
            )
        self.err = err.getvalue()
        return rc

    def detached(self, role="engineer"):
        name, argv, cwd, env = self.api.detached[-1]
        self.assertEqual((name, cwd), (f"agent-pm-{role}-{ID}", self.rd))
        expected = {
            **os.environ,
            "PATH": config.path(),
            "PYTHONPATH": os.pathsep.join(
                dict.fromkeys(
                    os.path.abspath(p)
                    for p in [
                        *PYTHONPATH.split(os.pathsep),
                        *os.environ.get("PYTHONPATH", "").split(os.pathsep),
                    ]
                    if p
                )
            ),
        }
        self.assertEqual(set(env), set(expected))
        for key, value in expected.items():
            self.assertTrue(env[key] == value, key)
        return argv

    def input(self, **kw):
        last = self.detached(**kw)[-1]
        self.assertTrue(last.startswith("--input="), last[:40])
        return last.removeprefix("--input=")


class TaskLabelsScenario(Base):
    ORPHAN = (
        'Task label "Orphan" is not one of researcher\'s tasks (deep-research, '
        "light-research). Fix the label or the assignee, then move the issue "
        "back to Todo."
    )

    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)


class TickScenario(TickBase):
    pass
