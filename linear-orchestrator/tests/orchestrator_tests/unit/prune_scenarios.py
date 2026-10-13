import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from unittest import mock

from agent_pm.linear_orchestrator.engine import promote, prune
from agent_pm.linear_orchestrator.linear import linear
from agent_pm.utils import sh
from orchestrator_tests.case import Case
from orchestrator_tests.unit import promote_support as tp
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.prune_data import (
    NOW,
    ROLES,
    TEAM_OBJ,
    git,
    write,
)
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL, logged, show


class Git:
    def __init__(self, real=False):
        self.real, self.calls = real, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if not self.real:
            raise AssertionError(f"git ran: {argv}")
        return sh(argv, timeout)


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
        linear.use_logs(self.logs)

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

    def prune(self, gql, dry=False, git=None, writable=None):
        before, err, out = (len(logged(self.logs)), io.StringIO(), io.StringIO())
        with redirect_stderr(err), redirect_stdout(out):
            code = prune.Pruner(
                gql,
                NOW,
                dry,
                work=self.work,
                driver=self.api,
                team=TEAM_OBJ,
                roles=ROLES,
                run=git or Git(),
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

    def real_clone(self, at=None):
        seed, bare, clone = (
            os.path.join(self.root, "seed"),
            os.path.join(self.root, "origin.git"),
            at or os.path.join(self.root, "clone"),
        )
        if not os.path.isdir(bare):
            git("init", "-q", "-b", "main", seed)
            git("-C", seed, "commit", "-q", "--allow-empty", "-m", "one")
            git("clone", "-q", "--bare", seed, bare)
        git("clone", "-q", bare, clone)
        return clone

    def git_env(self):
        return mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

    def tick(self, prune_gql):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f'work_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()
        fake.add("DR-1")
        fake.moved("DR-1", 60, "In Review")
        fake.said("DR-1", 45)
        fake.moved("DR-1", 30, "Handoff", frm=tp.STATES["In Review"])

        def gql(query, **v):
            self.user_queries += query == linear.Q_USER
            return (
                prune_gql if query in (prune.Q_ISSUE, prune.Q_FINISHED, prune.M_ARCHIVE) else fake
            )(query, **v)

        self.user_queries = 0
        before, err = (len(logged(self.logs)), io.StringIO())
        with redirect_stderr(err):
            code = promote.main(
                [], gql=gql, now=NOW, config_path=path, pruner=partial(prune.Pruner, run=Git())
            )
        self.events, self.err = (logged(self.logs)[before:], err.getvalue())
        return (code, fake.issues["DR-1"]["state"], "\n".join(show(e) for e in self.events))

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
