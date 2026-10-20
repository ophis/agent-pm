"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta

from agent_pm.delegate import ConfigError, SessionError, Spec
from agent_pm.orchestrator.engine import (
    config,
    log,
    promote,
    prune,
    runs,
)
from agent_pm.orchestrator.engine.types import LinearError, TrackerKind
from agent_pm.orchestrator.trackers.linear import (
    linear_tracker,
    ops,
)
from orchestrator_tests.case import Case
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.board_ids import DUPLICATE, FINISHED, TEAM, team_node
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL, logged, show

from delegate.tests.fakes.delegate import FakeDelegate
from delegate.tests.workdir import checkout_path

NOW = tp.NOW


STATE_IDS = {
    "Done": IDS_BY_KEY["done"],
    "Canceled": IDS_BY_KEY["canceled"],
    "Duplicate": DUPLICATE,
    "In Progress": IDS_BY_KEY["in_progress"],
    "Todo": IDS_BY_KEY["todo"],
    "In Review": IDS_BY_KEY["in_review"],
    "Handoff": IDS_BY_KEY["handoff"],
    "Backlog": IDS_BY_KEY["backlog"],
    "Triage": "00000000-0000-4000-8000-000000000019",  # no config key: read as state None
    "Elsewhere Done": "00000000-0000-4000-8000-000000000098",  # another team's
}


ROLES = {
    "u-researcher": "researcher",
    "u-pm": "pm",
    "u-engineer": "engineer",
    "u-dummy-tester": "dummy-tester",
}


TEAM_OBJ = ops.Team(TEAM, "Team", dict(IDS_BY_KEY), FINISHED)


STATE_TYPES = {
    "Done": "completed",
    "Canceled": "canceled",
    "Duplicate": "duplicate",
    "In Progress": "started",
    "Todo": "unstarted",
    "In Review": "started",
    "Handoff": "started",
    "Backlog": "backlog",
    "Triage": "triage",
    "Elsewhere Done": "completed",
}
CHILDREN = 50  # LinearTracker's archive read pages sub-issues by 50
PRUNE_QUERIES = (  # the queries gql_for answers
    linear_tracker.Q_ISSUE,
    linear_tracker.Q_SNAPSHOTS,
    linear_tracker.Q_CHANGED,
    linear_tracker.Q_FINISHED,
    linear_tracker.Q_ARCHIVE,
    linear_tracker.Q_CHILDREN,
    linear_tracker.M_ARCHIVE,
)


def gql_for(
    issues,
    owners=None,
    page=50,
    fail=(),
    refuse=(),
    *,
    children=None,
    stamps=None,
    reread=None,
    changed=None,
    elsewhere=(),
):
    """children: sub-issue state types by parent, any depth, as Linear lists them (archived ones
    left out); stamps: canceledAt and completedAt, hours before NOW, by issue (none: null);
    reread: the state an issue's archive read finds, by issue (none: its own); changed: the idents
    the change feed lists (gql.changed, settable later; None: every issue); elsewhere: issues the
    batched read leaves out (another team's). gql.read: the idents read, in order."""
    owners, children, stamps, reread = owners or {}, children or {}, stamps or {}, reread or {}

    def at(ident):
        given = stamps.get(ident, {})
        return {
            k: None if k not in given else (NOW - timedelta(hours=given[k])).isoformat()
            for k in ("canceledAt", "completedAt")
        }

    def subs(ident, after=None):
        kids, start = list(children.get(ident, {}).items()), int(after or 0)
        more = start + CHILDREN < len(kids)
        return {
            "nodes": [
                {"id": f"id-{c}", "identifier": c, "state": {"name": t.title(), "type": t}}
                for c, t in kids[start : start + CHILDREN]
            ],
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + CHILDREN) if more else None},
        }

    def planned(ident):
        state = reread.get(ident, issues[ident][0])
        return {
            "id": f"id-{ident}",
            "identifier": ident,
            "url": f"https://linear.app/t/issue/{ident}",
            "archivedAt": NOW.isoformat() if ident in gql.gone else None,
            "state": {"name": state, "type": STATE_TYPES[state]},
            "children": subs(ident),
        }

    def snapshot(ident):
        state = issues[ident][0]
        return {
            "state": {"id": STATE_IDS[state], "type": STATE_TYPES[state]},
            "history": history(ident),
            **at(ident),
        }

    def history(ident):
        return {
            "nodes": [
                {"createdAt": (NOW - timedelta(hours=h)).isoformat(), "toStateId": STATE_IDS[s]}
                for h, s in issues[ident][1]
            ]
        }

    def gql(query, **v):
        gql.calls.append(query)
        if query == ops.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == linear_tracker.Q_FINISHED:
            gql.finished.append(v)
            hits = [
                i
                for i, (state, _) in issues.items()
                if i not in gql.gone
                and (
                    (owners.get(i) in v["a"] and STATE_IDS[state] in v["d"])
                    or STATE_IDS[state] in v["s"]
                )
            ]
            start = int(v["c"] or 0)
            return {
                "issues": {
                    "nodes": [
                        {
                            "id": f"id-{i}",
                            "identifier": i,
                            "history": history(i),
                            **at(i),
                        }
                        for i in hits[start : start + page]
                    ],
                    "pageInfo": {
                        "hasNextPage": start + page < len(hits),
                        "endCursor": str(start + page),
                    },
                }
            }
        if query == linear_tracker.Q_ARCHIVE:
            return {"issue": planned(v["i"].removeprefix("id-"))}
        if query == linear_tracker.Q_CHILDREN:
            gql.subs.append(v["i"])
            return {"issue": {"children": subs(v["i"].removeprefix("id-"), v["c"])}}
        if query == linear_tracker.M_ARCHIVE:
            gql.archived.append(v["i"])
            ident = v["i"].removeprefix("id-")
            if ident in fail:
                raise LinearError("linear api error: boom")
            if ident not in refuse:
                gql.gone.add(ident)
            return {"issueArchive": {"success": ident not in refuse}}
        if query == linear_tracker.Q_SNAPSHOTS:
            numbers = v["f"]["number"]["in"]
            hits = [i for i in issues if int(i.rsplit("-", 1)[1]) in numbers and i not in elsewhere]
            gql.read += hits
            return {"issues": {"nodes": [{"identifier": i, **snapshot(i)} for i in hits]}}
        if query == linear_tracker.Q_CHANGED:
            listed = issues if gql.changed is None else gql.changed
            return {
                "issues": {
                    "nodes": [{"identifier": i} for i in sorted(listed)],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        gql.read.append(v["i"])
        if v["i"] not in issues:
            raise LinearError("linear api error: [{'message': 'Entity not found: Issue'}]")
        return {"issue": snapshot(v["i"])}

    gql.calls, gql.finished, gql.archived, gql.gone, gql.subs = [], [], [], set(), []
    gql.changed, gql.read = None if changed is None else set(changed), []
    gql.issues = issues
    return gql


def tree(path):
    found = {}
    for top, _, files in os.walk(path):
        for name in files:
            with open(os.path.join(top, name), "rb") as f:
                found[os.path.relpath(os.path.join(top, name), path)] = f.read()
    return found


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class Git:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        raise AssertionError(f"git ran: {argv}")


class PruneDelegate(FakeDelegate):
    owners = {}

    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        view = super().attach(record_or_workdir, sid=sid)
        view.owner = cls.owners[view.workdir]
        return view

    def close(self):
        self.owner.close(self.workdir)
        super().close()

    def status(self):
        if self.owner.unknown:
            raise SessionError("sessions unknown")
        return super().status()


class PruneScenario(Case):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(self.outside)
        write(os.path.join(self.outside, "keep"), "x")
        self.logs = os.path.join(self.root, "logs")
        self.api = FakeApi()
        self.sessions = {}
        self.closed_runs = set()
        self.unknown = False
        linear = config.LinearSettings(
            team=TEAM,
            states=dict(IDS_BY_KEY),
            human_members=(),
            harness_key="harness",
            manager_key="",
            task_label_group="",
            task_labels={},
            agent_label_group="",
            agent_labels={},
            project_repos={},
            roles={},
        )
        self.cfg = config.Config(
            task_tiers={},
            client_max_runs={},
            roles={},
            local_clones={},
            tracker=TrackerKind.LINEAR,
            linear=linear,
            board=None,
            work_dir=self.root,
            docs=config.Docs("", "", {}),
            stages={},
            delegate_kind="harness",
            delegates={"harness": PruneDelegate},
            core={},
        )
        log.use_logs(self.logs)

    def record(self, ident, sid, sessions, role="engineer", *, agent=True, hours=None):
        """hours: its runs.jsonl row that long before NOW, else now."""
        workdir = self.cfg.issue_workdir(ident)
        run = PruneDelegate("dummy", Spec("", workdir, sid=sid))
        if not agent:
            PruneDelegate.clean(workdir)
        run.detach()
        PruneDelegate.owners[workdir] = self
        self.addCleanup(PruneDelegate.owners.pop, workdir, None)
        self.sessions[workdir] = sessions
        runs.note(
            self.cfg.runs_log,
            "start",
            issue=ident,
            sid=sid,
            role=role,
            client="fake",
            delegate_kind="harness",
            **({} if hours is None else {"now": lambda: NOW - timedelta(hours=hours)}),
        )

    def close(self, workdir):
        if workdir in self.closed_runs:
            return
        failed = None
        for name in self.sessions[workdir]:
            try:
                self.api.close(name)
            except (ConfigError, SessionError) as error:
                failed = error
        if failed:
            raise failed
        self.closed_runs.add(workdir)

    def closed(self):
        return [c[1] for c in self.api.order if c[0] == "close"]

    def checkouts(self, ident):
        return PruneDelegate.checkouts(self.cfg.issue_workdir(ident))

    def fake_clone(self):
        clone = os.path.join(self.root, "clone")
        os.makedirs(os.path.join(clone, ".git", "worktrees", "x"))
        write(os.path.join(clone, ".git", "worktrees", "x", "commondir"), "../..\n")
        return clone

    def mkc(self, ident, owner="o", name="repo"):
        path = checkout_path(self.cfg.issue_workdir(ident), owner, name, ident)
        os.makedirs(os.path.join(path, ".git", "objects"))
        write(os.path.join(path, ".git", "HEAD"), "ref: refs/heads/main\n")
        pack = os.path.join(path, ".git", "objects", "pack.idx")
        write(pack, "x")
        os.chmod(pack, 292)
        write(os.path.join(path, "notes.md"), "uncommitted")
        os.symlink(self.outside, os.path.join(path, "out"))
        return path

    def assertOutsideKept(self):
        self.assertTrue(os.path.isfile(os.path.join(self.outside, "keep")))

    def prune(self, gql, git=None, writable=None, *, dry=False, now=NOW):
        before, err, out = (len(logged(self.logs)), io.StringIO(), io.StringIO())
        with redirect_stderr(err), redirect_stdout(out):
            code = prune.Pruner(
                gql,
                now,
                dry=dry,
                run=git or Git(),
                work=self.work,
                cfg=self.cfg,
                linear=self.cfg.linear,
                team=TEAM_OBJ,
                roles=ROLES,
                writable=writable,
            ).run()
        self.assertEqual(out.getvalue(), "")
        copies = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual({e["src"] for e in logged(self.logs)[before:] + copies} - {"prune"}, set())
        return (code, "\n".join(show(e) for e in logged(self.logs)[before:] + copies))

    def mkg(self, ident, owner="o", name="n"):
        path = checkout_path(self.cfg.issue_workdir(ident), owner, name, ident)
        os.makedirs(path)
        write(os.path.join(path, ".git"), f"gitdir: {self.root}/clone/.git/worktrees/x\n")
        return path

    def fresh(self):
        shutil.rmtree(self.work)
        os.makedirs(self.work)

    def tick(self, prune_gql):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()
        fake.add("DR-1")
        fake.moved("DR-1", 60, "In Review")
        fake.said("DR-1", 45)
        fake.moved("DR-1", 30, "Handoff", frm=tp.STATES["In Review"])

        def gql(query, **v):
            self.user_queries += query == ops.Q_USER
            return (prune_gql if query in PRUNE_QUERIES else fake)(query, **v)

        self.user_queries = 0
        before, err = (len(logged(self.logs)), io.StringIO())
        with redirect_stderr(err):
            code = promote.main([], gql=gql, now=NOW, config_path=path, pruner=self.pruner)
        self.events, self.err = (logged(self.logs)[before:], err.getvalue())
        return (code, fake.issues["DR-1"]["state"], "\n".join(show(e) for e in self.events))

    def pruner(self, *args, **kwargs):
        cfg = replace(kwargs["cfg"], delegates={"harness": PruneDelegate})
        return prune.Pruner(*args, **{**kwargs, "run": Git(), "cfg": cfg})

    def outage(self, op, issues, failed):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                for said in ([("prune", f"linear-error op={op} {FIELDS[name]}")], []):
                    gql = failing(error, gql=issues, ops={op})
                    self.assertEqual(self.tick(gql)[:2], (0, "Done"))
                    self.assertEqual(
                        ([(e["src"], show(e)) for e in self.events], self.err),
                        ([("promote", "promote DR-1 to=C-1"), *said], ""),
                    )
                    self.assertEqual(gql.failed, failed)
