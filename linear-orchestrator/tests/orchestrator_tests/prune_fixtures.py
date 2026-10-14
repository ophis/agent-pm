"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta

from agent_pm.delegate import ConfigError, DriverError, Spec
from agent_pm.linear_orchestrator.engine import config, log, ops, promote, prune, runs
from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_tests.case import Case
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import TEAM, team_node
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL, logged, show

from delegate.tests.fakes.delegate import FakeDelegate

NOW = tp.NOW


STATE_IDS = {
    "Done": IDS_BY_KEY["done"],
    "Canceled": IDS_BY_KEY["canceled"],
    "In Progress": IDS_BY_KEY["in_progress"],
}


ROLES = {
    "u-researcher": "researcher",
    "u-pm": "pm",
    "u-engineer": "engineer",
    "u-dummy-tester": "dummy-tester",
}


TEAM_OBJ = ops.Team(TEAM, "Team", dict(IDS_BY_KEY))


def gql_for(issues, owners=None, page=50, fail=(), refuse=()):
    owners = owners or {}

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
        if query == prune.Q_FINISHED:
            gql.finished.append(v)
            hits = [
                i
                for i, (state, _) in issues.items()
                if i not in gql.gone and owners.get(i) in v["a"] and STATE_IDS[state] in v["s"]
            ]
            start = int(v["c"] or 0)
            return {
                "issues": {
                    "nodes": [
                        {"id": f"id-{i}", "identifier": i, "history": history(i)}
                        for i in hits[start : start + page]
                    ],
                    "pageInfo": {
                        "hasNextPage": start + page < len(hits),
                        "endCursor": str(start + page),
                    },
                }
            }
        if query == prune.M_ARCHIVE:
            gql.archived.append(v["i"])
            ident = v["i"].removeprefix("id-")
            if ident in fail:
                raise LinearError("linear api error: boom")
            if ident not in refuse:
                gql.gone.add(ident)
            return {"issueArchive": {"success": ident not in refuse}}
        return {
            "issue": {"state": {"id": STATE_IDS[issues[v["i"]][0]]}, "history": history(v["i"])}
        }

    gql.calls, gql.finished, gql.archived, gql.gone = [], [], [], set()
    return gql


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
    def attach(cls, record_or_workdir):
        view = super().attach(record_or_workdir)
        view.owner = cls.owners[view.workdir]
        return view

    def close(self):
        self.owner.close(self.workdir)
        super().close()

    def status(self):
        if self.owner.unknown:
            raise DriverError("sessions unknown")
        return super().status()


class PruneScenario(Case):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(os.path.join(self.outside, "dotgit"))
        write(os.path.join(self.outside, "keep"), "x")
        self.logs = os.path.join(self.root, "logs")
        self.api = FakeApi()
        self.sessions = {}
        self.closed_runs = set()
        self.unknown = False
        self.cfg = config.Config(
            team=TEAM,
            states=dict(IDS_BY_KEY),
            human_members=(),
            harness_key="harness",
            task_label_group="",
            task_labels={},
            roles={},
            project_repos={},
            local_clones={},
            work_dir=self.root,
            docs=config.Docs("", "", {}),
            stages={},
            delegate_kind="harness",
            delegates={"harness": PruneDelegate},
            core={},
        )
        log.use_logs(self.logs)

    def record(self, ident, sid, sessions, role="engineer"):
        workdir = self.cfg.run_dir(ident)
        PruneDelegate("dummy", Spec("", workdir, sid=sid)).detach()
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
        )

    def close(self, workdir):
        if workdir in self.closed_runs:
            return
        failed = None
        for name in self.sessions[workdir]:
            try:
                self.api.close(name)
            except (ConfigError, DriverError) as error:
                failed = error
        if failed:
            raise failed
        self.closed_runs.add(workdir)

    def closed(self):
        return [c[1] for c in self.api.order if c[0] == "close"]

    def mkc(self, ident, *parts):
        path = os.path.join(self.work, ident, *parts)
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

    def prune(self, gql, git=None, writable=None, *, dry=False):
        before, err, out = (len(logged(self.logs)), io.StringIO(), io.StringIO())
        with redirect_stderr(err), redirect_stdout(out):
            code = prune.Pruner(
                gql,
                NOW,
                dry=dry,
                run=git or Git(),
                work=self.work,
                cfg=self.cfg,
                team=TEAM_OBJ,
                roles=ROLES,
                writable=writable,
            ).run()
        self.assertEqual(out.getvalue(), "")
        copies = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual({e["src"] for e in logged(self.logs)[before:] + copies} - {"prune"}, set())
        return (code, "\n".join(show(e) for e in logged(self.logs)[before:] + copies))

    def mkg(self, ident, *parts, git="file"):
        path = os.path.join(self.work, ident, *parts)
        os.makedirs(path)
        if git:
            write(os.path.join(path, ".git"), f"gitdir: {self.root}/clone/.git/worktrees/x\n")
        return path

    def fresh(self):
        shutil.rmtree(self.work)
        os.makedirs(self.work)

    def symlinked_dot_git(self, ident, *parts):
        path = self.mkc(ident, *parts)
        shutil.rmtree(os.path.join(path, ".git"))
        os.symlink(os.path.join(self.outside, "dotgit"), os.path.join(path, ".git"))
        return path

    def tick(self, prune_gql):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f'[linear_orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
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
            return (
                prune_gql if query in (prune.Q_ISSUE, prune.Q_FINISHED, prune.M_ARCHIVE) else fake
            )(query, **v)

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
