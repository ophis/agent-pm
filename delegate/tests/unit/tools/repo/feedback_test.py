"""Feedback behavior."""

import io
import json
import os
import tempfile

from agent_pm.delegate.team.tools.types import RepoServices

from delegate.tests.case import Case
from delegate.tests.unit.tools.repo.case import (
    LOCAL,
    LOGIN,
    NO_FEEDBACK,
    REPO_API,
    Base,
    Fake,
    info,
    ok,
    pr,
    pr_list,
    repo,
)


class PlanDocs(Case):
    def test_nested_checkouts_node_modules_and_git_are_skipped_but_not_dot_dirs(self):
        """A subdir holding a .git file (a worktree) or dir (a clone) is a checkout of its own."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = tmp.name

        def doc(*parts):
            path = os.path.join(wt, *parts, "plan.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("RESUME: phase=S5 branch=b\n")
            return path

        found = doc("docs", ".autopilot")
        for parts in (
            ("wt", "x", "worktrees", "y"),
            ("vendor", "clone"),
            ("node_modules", "p"),
            (".git",),
        ):
            doc(*parts)
        with open(os.path.join(wt, "wt", "x", "worktrees", "y", ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/y\n")
        os.makedirs(os.path.join(wt, "vendor", "clone", ".git"))
        self.assertEqual(repo.plan_docs(wt, "b"), [{"path": found, "phase": "S5"}])


class Status(Base):
    def plan_doc(self, branch="TASK-1-x", phase="S8"):
        os.makedirs(os.path.join(self.wt, "docs"), exist_ok=True)
        path = os.path.join(self.wt, "docs", "plan.md")
        with open(path, "w") as f:
            f.write(f"# Plan\n\nRESUME: phase={phase} branch={branch}\n")
        return path

    def status(self, *answers, argv=(), config=None, local=None):
        """(exit code, the output or stderr, run) of status on a checkout of o/n; `answers` after
        the gh login's.
        """
        run = Fake([self.existing(), LOGIN, *answers])
        code, out, err = self.main(
            ["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir, *argv], run, config, local
        )
        return code, json.loads(out) if code == 0 else err, run

    def test_pr_plan_docs_and_feedback_since_the_latest_plan_doc_commit(self):
        path = self.plan_doc()
        comments = [
            [
                {"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"},
            ]
        ]
        reviews = [
            [
                {
                    "submitted_at": "2026-10-03T01:00:00Z",
                    "user": {"login": "bot"},
                    "body": "nit",
                    "state": "COMMENTED",
                },
                {"submitted_at": None, "user": {"login": "bot"}, "body": "pending"},
            ]
        ]
        code, r, run = self.status(
            (lambda a: a[3:5] == ["log", "-1"], ok("2026-10-02T00:00:00+00:00\n")),
            pr_list(
                [
                    {
                        "number": 9,
                        "url": "u9",
                        "state": "OPEN",
                        "isCrossRepository": True,
                        "author": {"login": "me"},
                    },
                    pr("TASK-0-a"),
                ]
            ),
            (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
            (lambda a: a[-1].endswith("/pulls/7/reviews"), ok(json.dumps(reviews))),
            (lambda a: a[-1].endswith("/pulls/7/comments"), ok("[[]]")),
        )
        self.assertEqual(code, 0, r)
        self.assertEqual(
            r["pr"], {"number": 7, "url": "u7", "state": "OPEN", "baseRefName": "TASK-0-a"}
        )
        self.assertIn(
            "number,url,state,baseRefName,isCrossRepository,author",
            next(c for c in run.calls if c[1] == "pr"),
        )
        self.assertEqual(r["plan_docs"], [{"path": path, "phase": "S8"}])
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([(e["body"], e["state"]) for e in r["others"]], [("nit", "COMMENTED")])

    def test_configured_users_replace_the_gh_login(self):
        """From the config and local passed only, never the HOME one (malformed here)."""
        comments = [
            [
                {
                    "created_at": "2026-10-03T00:00:00Z",
                    "user": {"login": "me"},
                    "body": "from the bot",
                },
                {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "Alice"}, "body": "do Y"},
            ]
        ]
        with open(os.path.join(self.agent_pm, LOCAL), "w") as f:
            f.write("malformed = [\n")
        local = os.path.join(self.dir, "config.local.toml")
        with open(local, "w") as f:
            f.write('users = ["alice"]\n')
        for text, given in (('users = ["alice"]\n', os.devnull), ('users = ["bob"]\n', local)):
            with self.subTest(local=given):
                code, r, _ = self.status(
                    pr_list([pr()]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                    config=self.config(text),
                    local=given,
                )
                self.assertEqual((code, r["pr"]["number"]), (0, 7), r)
                self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
                self.assertEqual([e["body"] for e in r["others"]], ["from the bot"])
        with self.subTest("users = [] is the gh login"):
            code, r, _ = self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                NO_FEEDBACK,
                config=self.config("users = []\n"),
                local=os.devnull,
            )
            self.assertEqual((code, [e["body"] for e in r["user"]]), (0, ["from the bot"]), r)
            self.assertEqual([e["body"] for e in r["others"]], ["do Y"])

    def test_other_branches_plan_docs_and_no_pr(self):
        self.plan_doc(branch="other")
        self.assertEqual(
            self.status(pr_list([]), info())[:2],
            (
                0,
                {
                    "pr": None,
                    "base": "main",
                    "plan_docs": [],
                    "since": None,
                    "user": [],
                    "others": [],
                },
            ),
        )

    def test_an_invalid_trusted_dirs_does_not_affect_status(self):
        code, err, _ = self.status(
            pr_list([]), info(), config=self.config('trusted_dirs = ["a/b"]\n')
        )
        self.assertEqual(code, 0, err)

    def test_bad_config_is_reported_before_an_unreadable_repo(self):
        code, _, _ = self.main(
            ["status", "--dir", self.dir, "--branch", "b", "not a repo"],
            Fake([]),
            self.config("users = [\n"),
        )
        self.assertEqual(code, 1)

    def test_the_defaults_load_the_local_file(self):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as stream:
            stream.write("users = [\n")
        errors = io.StringIO()
        code = repo.main(
            ["status", "o/n", "--branch", "b", "--dir", self.dir],
            services=RepoServices(run=Fake([]), out=io.StringIO(), err=errors),
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid value", errors.getvalue())

    def test_needs_a_checkout(self):
        code, _, err = self.main(["status", "o/n", "--branch", "b", "--dir", self.dir], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("run worktree first", err)

    def test_base_is_the_prs_else_the_option_else_the_default(self):
        for prs, argv, want, asks in (
            ([pr("TASK-0-a")], ["--base", "TASK-0-b"], "TASK-0-a", False),
            ([], ["--base", "TASK-0-b"], "TASK-0-b", False),
            ([], [], "trunk", True),
        ):
            with self.subTest(prs=prs, argv=argv):
                code, r, run = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default="trunk"), argv=argv
                )
                self.assertEqual((code, r["base"]), (0, want), r)
                self.assertEqual(run.ran(*REPO_API), asks)

    def test_an_unsafe_base_exits_2(self):
        for prs, argv, default in (
            ([pr("a..b")], [], "main"),
            ([pr(None)], [], "main"),
            ([], ["--base", "x;rm -rf ~"], "main"),
            ([], ["--base", ""], "main"),
            ([], [], "-x"),
        ):
            with self.subTest(prs=prs, argv=argv, default=default):
                code, err, _ = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default=default), argv=argv
                )
                self.assertEqual(code, 2, err)
                self.assertIn("unsafe base branch name", err)
