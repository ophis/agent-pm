import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import repo  # noqa: E402

SHA = "a" * 40


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class Fake:
    """A `run` that answers by argv prefix and records every call; `gh repo clone` makes the checkout's .git."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[:3] == ["gh", "repo", "clone"]:
            os.makedirs(os.path.join(argv[4], ".git"))
        for prefix, res in self.answers:
            if prefix(argv) if callable(prefix) else argv[:len(prefix)] == prefix:
                return res
        return ok()

    def ran(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


def info(push=True, default="main"):
    return (["gh", "api", "--hostname"], ok(json.dumps({"permissions": {"push": push}, "default_branch": default})))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.wt = os.path.join(self.dir, "n")

    def existing(self, url="https://github.com/o/n.git"):
        os.makedirs(os.path.join(self.wt, ".git"))
        return (lambda a: a[3:] == ["remote", "get-url", "origin"], ok(url + "\n"))

    def main(self, argv, run):
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(argv, run=run, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_forms(self):
        for spec, want in (("o/n", ("github.com", "o", "n")), ("ghe.example.com/o/n", ("ghe.example.com", "o", "n")),
                           ("https://github.com/o/n.git", ("github.com", "o", "n")), ("https://GHE.io/o/n/", ("ghe.io", "o", "n"))):
            self.assertEqual(repo.Repo.parse(spec), repo.Repo(*want), spec)

    def test_unreadable(self):
        for spec in ("n", "o/..", "https://o/n", "o/n; rm -rf /", "-o/n"):
            with self.assertRaises(repo.Invalid, msg=spec):
                repo.Repo.parse(spec)

    def test_origin(self):
        self.assertEqual(repo.origin("git@GitHub.com:O/N.git"), "github.com/o/n")
        self.assertEqual(repo.origin("https://ghe.io/o/n"), "ghe.io/o/n")


class Prepare(Base):
    def test_fresh_shallow_detached_checkout(self):
        run = Fake([info(), (["git", "-C", self.wt, "rev-parse"], ok(SHA + "\n"))])
        code, out, _ = self.main(["prepare", "o/n", "--dir", self.dir], run)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"repo": "o/n", "host": "github.com", "commit": SHA, "worktree": self.wt,
                                           "permalink_base": f"https://github.com/o/n/blob/{SHA}/"})
        self.assertIn(["gh", "repo", "clone", "github.com/o/n", self.wt, "--", "-c", "core.symlinks=false", "--depth", "1"],
                      run.calls)
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--detach"))

    def test_reuses_a_checkout_of_the_same_repo(self):
        run = Fake([self.existing(), (["git", "-C", self.wt, "rev-parse"], ok(SHA))])
        self.assertEqual(self.main(["prepare", "https://github.com/o/n", "--dir", self.dir], run)[0], 0)
        self.assertFalse(run.ran("gh"))

    def test_other_repo_in_the_way_is_invalid(self):
        code, _, err = self.main(["prepare", "o/n", "--dir", self.dir], Fake([self.existing("https://github.com/x/n")]))
        self.assertEqual(code, 2)
        self.assertIn("not a checkout of github.com/o/n", err)

    def test_not_found_is_invalid(self):
        run = Fake([(["gh", "api"], fail("gh: Not Found (HTTP 404)"))])
        code, _, err = self.main(["prepare", "o/n", "--dir", self.dir], run)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_a_second_dir_is_refused(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", err):
            repo.main(["prepare", "--dir", self.dir, "o/n", "--dir", os.path.expanduser("~/.claude/skills")], run=Fake([]))
        self.assertIn("--dir given twice", err.getvalue())

    def test_network_failure_exits_1(self):
        run = Fake([info(), (["gh", "repo", "clone"], fail("connection reset"))])
        code, _, err = self.main(["prepare", "o/n", "--dir", self.dir], run)
        self.assertEqual(code, 1)
        self.assertIn("connection reset", err)


class Checkout(Base):
    def checkout(self, *answers, branch="TASK-1-x"):
        run = Fake(list(answers))
        return self.main(["checkout", "o/n", f"--branch={branch}", "--dir", self.dir], run), run

    def test_new_branch_from_the_default(self):
        (code, out, _), run = self.checkout(info(default="trunk"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"repo": "o/n", "host": "github.com", "default": "trunk", "branch": "TASK-1-x",
                                           "worktree": self.wt})
        self.assertIn(["gh", "repo", "clone", "github.com/o/n", self.wt, "--", "-c", "core.symlinks=false"], run.calls)
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/trunk"))

    def test_remote_branch_is_tracked(self):
        (code, _, _), run = self.checkout(info(), (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, "ls-remote", "--heads", "origin", "refs/heads/TASK-1-x"))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--track", "-b", "TASK-1-x", "origin/TASK-1-x"))

    def test_default_branch_is_invalid(self):
        (code, _, err), run = self.checkout(info(default="main"), branch="main")
        self.assertEqual(code, 2)
        self.assertIn("default branch", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_existing_checkout_fetches_and_switches_to_the_local_branch(self):
        (code, _, _), run = self.checkout(self.existing(), info(), (lambda a: a[3:5] == ["branch", "--list"], ok("  TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, "fetch", "origin"))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "TASK-1-x"))
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_already_on_the_branch_changes_nothing(self):
        (code, _, _), run = self.checkout(self.existing(), info(), (lambda a: "--show-current" in a, ok("TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertFalse(any("checkout" in c for c in run.calls))

    def test_unsafe_branch_is_reported_before_an_unreadable_repo(self):
        code, _, err = self.main(["checkout", "--dir", self.dir, "--branch=a..b", "not a repo"], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("unsafe branch name", err)

    def test_no_push_permission_is_invalid(self):
        (code, _, err), run = self.checkout(info(push=False))
        self.assertEqual(code, 2)
        self.assertIn("no push permission", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_unsafe_branch_is_invalid(self):
        for branch in ("-x", "a..b", "a b", "x/"):
            (code, _, _), run = self.checkout(info(), branch=branch)
            self.assertEqual(code, 2, branch)
            self.assertEqual(run.calls, [])


class Status(Base):
    def plan_doc(self, branch="TASK-1-x", phase="S9"):
        os.makedirs(os.path.join(self.wt, "docs"), exist_ok=True)
        path = os.path.join(self.wt, "docs", "plan.md")
        with open(path, "w") as f:
            f.write(f"# Plan\n\nRESUME: phase={phase} branch={branch}\n")
        return path

    def test_pr_plan_docs_and_feedback_since_the_latest_plan_doc_commit(self):
        path = self.plan_doc()
        comments = [[{"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                     {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"}]]
        reviews = [[{"submitted_at": "2026-10-03T01:00:00Z", "user": {"login": "bot"}, "body": "nit", "state": "COMMENTED"},
                    {"submitted_at": None, "user": {"login": "bot"}, "body": "pending"}]]
        run = Fake([self.existing(),
                    (lambda a: a[3:5] == ["log", "-1"], ok("2026-10-02T00:00:00+00:00\n")),
                    (["gh", "api", "--hostname", "github.com", "user"], ok(json.dumps({"login": "me"}))),
                    (["gh", "pr", "list"], ok(json.dumps([
                        {"number": 9, "url": "u9", "state": "OPEN", "isCrossRepository": True, "author": {"login": "me"}},
                        {"number": 7, "url": "u7", "state": "OPEN", "isCrossRepository": False, "author": {"login": "me"}}]))),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    (lambda a: a[-1].endswith("/pulls/7/reviews"), ok(json.dumps(reviews))),
                    (lambda a: a[-1].endswith("/pulls/7/comments"), ok("[[]]"))])
        code, out, _ = self.main(["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir], run)
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["pr"], {"number": 7, "url": "u7", "state": "OPEN"})
        self.assertEqual(r["plan_docs"], [{"path": path, "phase": "S9"}])
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([(e["body"], e["state"]) for e in r["others"]], [("nit", "COMMENTED")])

    def test_configured_users_replace_the_gh_login(self):
        comments = [[{"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "from the bot"},
                     {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "Alice"}, "body": "do Y"}]]
        run = Fake([self.existing(), (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                    (["gh", "pr", "list"], ok(json.dumps([{"number": 7, "url": "u7", "state": "OPEN",
                                                           "isCrossRepository": False, "author": {"login": "me"}}]))),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    (lambda a: "--paginate" in a, ok("[[]]"))])
        config = os.path.join(self.dir, "config.toml")
        with open(config, "w") as f:
            f.write('users = ["alice"]\n')
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(["status", "o/n", "--branch", "b", "--dir", self.dir], run=run, out=out, err=err, config=config)
        r = json.loads(out.getvalue())
        self.assertEqual((code, r["pr"]["number"]), (0, 7))
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([e["body"] for e in r["others"]], ["from the bot"])

    def test_other_branches_plan_docs_and_no_pr(self):
        self.plan_doc(branch="other")
        run = Fake([self.existing(), (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                    (["gh", "pr", "list"], ok("[]"))])
        code, out, _ = self.main(["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir], run)
        self.assertEqual((code, json.loads(out)), (0, {"pr": None, "plan_docs": [], "since": None, "user": [], "others": []}))

    def test_bad_config_is_reported_before_an_unreadable_repo(self):
        config = os.path.join(self.dir, "config.toml")
        with open(config, "w") as f:
            f.write("users = [\n")
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(["status", "--dir", self.dir, "--branch", "b", "not a repo"], run=Fake([]), out=out, err=err,
                         config=config)
        self.assertEqual(code, 1)

    def test_needs_a_checkout(self):
        code, _, err = self.main(["status", "o/n", "--branch", "b", "--dir", self.dir], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("run checkout first", err)


if __name__ == "__main__":
    unittest.main()
