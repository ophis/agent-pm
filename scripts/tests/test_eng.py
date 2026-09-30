import dataclasses, io, json, os, sys, subprocess, tempfile, unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eng  # noqa: E402
import pipeline  # noqa: E402

def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

class FakeRun:
    """Maps argv prefixes (tuples) to results; records calls."""
    def __init__(self, table):
        self.table, self.calls = table, []
    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        for prefix, res in self.table:
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(res, BaseException):
                    raise res
                return res
        raise AssertionError(f"unexpected {argv}")

PROJ = "121166b1-191a-4461-bec4-42f1c2dc0ddd"
NO_LINE = eng.Invalid("no Repo: line in the description or its ## Instructions")

def gql_for(description, title="ENG: Session Registry", ident="TASK-26", project=None):
    return lambda q, **v: {"issue": {"identifier": ident, "title": title, "description": description, "project": project}}

def pr_row(number, login="ophis", fork=False):
    return {"number": number, "url": f"u{number}", "state": "OPEN", "isCrossRepository": fork, "author": {"login": login}}

class Placeholders(unittest.TestCase):
    def test_placeholders_are_ok_fields(self):
        self.assertLessEqual(pipeline.PLACEHOLDERS, {f.name for f in dataclasses.fields(eng.Ok)})

class ParseRepo(unittest.TestCase):
    def test_forms(self):
        for line in ("Repo: ophis/agent-pm", "repo: https://github.com/ophis/agent-pm",
                     "REPO: https://github.com/ophis/agent-pm.git/", "Repo: git@github.com:ophis/agent-pm.git",
                     "repo: [https://github.com/ophis/agent-pm](<https://github.com/ophis/agent-pm>)",
                     "Repo: [agent-pm](https://github.com/ophis/agent-pm)", "  repo:   ophis/agent-pm  "):
            self.assertEqual(eng.parse_repo(f"x\n{line}\ny"), ("ophis", "agent-pm"), line)

    def test_crlf(self):
        self.assertEqual(eng.parse_repo("a\r\nRepo: ophis/agent-pm\r\nb"), ("ophis", "agent-pm"))

    def test_comments_section_ignored(self):
        d = "Repo: ophis/a\n\n## Comments\n* x:\n  > Repo: ophis/b"
        self.assertEqual(eng.parse_repo(d), ("ophis", "a"))
        self.assertIsInstance(eng.parse_repo("## Comments\nRepo: ophis/a"), eng.Invalid)

    def test_same_value_twice_ok_two_values_invalid(self):
        self.assertEqual(eng.parse_repo("Repo: ophis/a\nrepo: OPHIS/A"), ("ophis", "a"))
        self.assertIsInstance(eng.parse_repo("Repo: ophis/a\nRepo: ophis/b"), eng.Invalid)

    def test_name_starting_with_dot(self):
        self.assertEqual(eng.parse_repo("Repo: ophis/.github"), ("ophis", ".github"))
        self.assertEqual(eng.parse_repo("Repo: https://github.com/ophis/.github.git"), ("ophis", ".github"))
        self.assertEqual(eng.parse_repo("Repo: ophis/..x"), ("ophis", "..x"))

    def test_bad_values(self):
        for v in ("", "ophis", "-ophis/a", "ophis/-a", "ophis/..", "ophis/.", "a b/c", "ophis/a;rm", "https://gitlab.com/ophis/a"):
            self.assertIsInstance(eng.parse_repo(f"Repo: {v}"), eng.Invalid, v)

class Slug(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(eng.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(eng.slug("Engineering 骨架"), "engineering")
        self.assertEqual(eng.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(eng.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(eng.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))

class Resolve(unittest.TestCase):
    def setUp(self):
        self.pg = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.pg]))
        self.clone = os.path.join(self.pg, "agent-pm")
        self.work = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.work]))
        patcher = mock.patch.object(pipeline, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def table(self, **over):
        t = {
            "api": ok('{"default_branch": "main", "permissions": {"push": true}}'),
            "clone": ok(),
            "wt": ok("true\n"),
            "fetch_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "push_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "branches": ok(""),
            "remote": ok(""),
        }
        t.update(over)
        return [
            (("gh", "api", "repos/ophis/agent-pm"), t["api"]),
            (("gh", "repo", "clone"), t["clone"]),
            (("git", "-C", self.clone, "rev-parse", "--is-inside-work-tree"), t["wt"]),
            (("git", "-C", self.clone, "remote", "get-url", "--push", "origin"), t["push_url"]),
            (("git", "-C", self.clone, "remote", "get-url", "origin"), t["fetch_url"]),
            (("git", "-C", self.clone, "branch", "--list"), t["branches"]),
            (("git", "-C", self.clone, "ls-remote", "--heads", "origin"), t["remote"]),
        ]

    def resolve(self, desc="Repo: ophis/agent-pm", gql=None, repos=None, project=None, **over):
        self.run_ = FakeRun(self.table(**over))
        extra = {} if repos is None else {"repos": repos}
        return eng.resolve("TASK-26", gql or gql_for(desc, project=project), self.run_, playground=self.pg, **extra)

    def mapped(self, desc="no repo here", **over):
        return self.resolve(desc, repos={PROJ: "ophis/agent-pm"}, project={"id": PROJ}, **over)

    def test_ok_existing_clone(self):
        os.makedirs(self.clone)
        r = self.resolve()
        self.assertEqual(r, eng.Ok("TASK-26", "ENG: Session Registry", "ophis", "agent-pm", self.clone, "main",
                                   "TASK-26-session-registry",
                                   os.path.join(self.work, "TASK-26", "worktrees", "TASK-26-session-registry")))

    def test_absent_clone_is_cloned(self):
        r = self.resolve()
        self.assertIsInstance(r, eng.Ok)
        self.assertIn((("gh", "repo", "clone", "ophis/agent-pm", self.clone), 600), self.run_.calls)

    def test_linear_error_transient(self):
        def boom(q, **v):
            raise SystemExit("linear api error: boom")
        self.assertIsInstance(eng.resolve("TASK-26", boom, FakeRun([]), playground=self.pg), eng.Transient)

    def test_issue_not_found_or_other_identifier_invalid(self):
        self.assertEqual(self.resolve(gql=lambda q, **v: {"issue": None}), eng.Invalid("TASK-26: issue not found"))
        for ident in ("OPS-26", "task-26"):
            self.assertIsInstance(self.resolve(gql=gql_for("Repo: ophis/agent-pm", ident=ident)), eng.Invalid, ident)
            self.assertEqual(self.run_.calls, [])

    def test_access(self):
        os.makedirs(self.clone)
        for res, kind in ((ok(code=1, stderr="gh: Not Found (HTTP 404)"), eng.Invalid),
                          (ok(code=1, stderr="gh: Forbidden (HTTP 403)"), eng.Invalid),
                          (ok('{"default_branch": "main", "permissions": {"push": false}}'), eng.Invalid),
                          (ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"), eng.Transient),
                          (ok(code=1, stderr="error connecting to api.github.com"), eng.Transient),
                          (subprocess.TimeoutExpired("gh", 60), eng.Transient),
                          (FileNotFoundError("gh"), eng.Transient),
                          (ok('{"default_branch": "-x", "permissions": {"push": true}}'), eng.Invalid),
                          (ok('{"default_branch": "a..b", "permissions": {"push": true}}'), eng.Invalid),
                          (ok('{"default_branch": "ma$(x)", "permissions": {"push": true}}'), eng.Invalid),
                          (ok('{"default_branch": null, "permissions": {"push": true}}'), eng.Invalid),
                          (ok("<html>502</html>"), eng.Transient), (ok("[]"), eng.Transient),
                          (ok('{"permissions": ["push"]}'), eng.Transient)):
            self.assertIsInstance(self.resolve(api=res), kind, res)

    def test_clone_checks(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", other]))
        os.symlink(other, self.clone)
        self.assertIsInstance(self.resolve(), eng.Invalid)
        os.remove(self.clone)
        os.makedirs(self.clone)
        self.assertIsInstance(self.resolve(wt=ok(code=128, stderr="not a git repository")), eng.Invalid)
        self.assertIsInstance(self.resolve(fetch_url=ok("git@github.com:other/agent-pm.git\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(push_url=ok("https://evil.example/x.git\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(fetch_url=ok("https://github.com/OPHIS/Agent-PM\n")), eng.Ok)

    def test_clone_failure_transient(self):
        self.assertIsInstance(self.resolve(clone=ok(code=1, stderr="network")), eng.Transient)
        self.assertIsInstance(self.resolve(clone=subprocess.TimeoutExpired("gh", 600)), eng.Transient)

    def test_existing_branches(self):
        os.makedirs(self.clone)
        self.assertEqual(self.resolve(branches=ok("TASK-26-old-name\n")).branch, "TASK-26-old-name")
        self.assertEqual(self.resolve(remote=ok("abc\trefs/heads/TASK-26-remote\n")).branch, "TASK-26-remote")
        self.assertIsInstance(self.resolve(branches=ok("TASK-26-a\nTASK-26-b\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(branches=ok("TASK-26-Bad$(x)\n")), eng.Invalid)

    def test_branch_listing_failure_transient(self):
        os.makedirs(self.clone)
        for over in ({"branches": ok(code=128, stderr="fatal")}, {"remote": ok(code=128, stderr="no remote")},
                     {"remote": subprocess.TimeoutExpired("git", 60)}):
            self.assertIsInstance(self.resolve(**over), eng.Transient, over)

    def test_local_only_branch_with_worktree_present_resolves_without_remote_lookup(self):
        """Crash after `worktree add`, before the first push: the worktree already exists on disk."""
        os.makedirs(self.clone)
        worktree = os.path.join(self.work, "TASK-26", "worktrees", "TASK-26-old-name")
        os.makedirs(worktree)
        r = self.resolve(branches=ok("TASK-26-old-name\n"))
        self.assertEqual((r.branch, r.worktree), ("TASK-26-old-name", worktree))
        remote_prefix = ("git", "-C", self.clone, "ls-remote", "--heads", "origin")
        self.assertFalse(any(c[0][:len(remote_prefix)] == remote_prefix for c in self.run_.calls))

    def test_invalid_repo_line(self):
        self.assertIsInstance(self.resolve(desc="no repo here"), eng.Invalid)

    def test_mapped_project_without_repo_line(self):
        os.makedirs(self.clone)
        asked = []
        def gql(q, **v):
            asked.append(q)
            return gql_for("no repo here", project={"id": PROJ})(q, **v)
        r = self.resolve(gql=gql, repos={PROJ: "ophis/agent-pm"})
        self.assertEqual(r, eng.Ok("TASK-26", "ENG: Session Registry", "ophis", "agent-pm", self.clone, "main",
                                   "TASK-26-session-registry",
                                   os.path.join(self.work, "TASK-26", "worktrees", "TASK-26-session-registry"), mapped=True))
        self.assertIn((("gh", "api", "repos/ophis/agent-pm"), 60), self.run_.calls)
        self.assertIn("project { id }", asked[0])

    def test_repo_line_wins_over_mapping(self):
        os.makedirs(self.clone)
        r = self.resolve(repos={PROJ: "ophis/other"}, project={"id": PROJ})
        self.assertEqual((r.owner, r.name, r.mapped), ("ophis", "agent-pm", False))
        self.assertNotIn(("gh", "api", "repos/ophis/other"), [c[0] for c in self.run_.calls])

    def test_bad_repo_line_is_not_replaced_by_mapping(self):
        self.assertEqual(self.mapped("Repo: nope"), eng.Invalid("unreadable Repo line: 'nope'"))
        self.assertEqual(self.mapped("Repo: ophis/a\nRepo: ophis/b"), eng.Invalid("several different Repo: values"))
        self.assertEqual(self.run_.calls, [])

    def test_no_mapping_for_the_project_keeps_todays_invalid(self):
        for project in ({"id": "other"}, None, "not-an-object", {"id": ["x"]}, {"id": None}, {}, {"id": 7}):
            with self.subTest(project=project):
                self.assertEqual(self.resolve(desc="no repo here", repos={PROJ: "ophis/agent-pm"}, project=project), NO_LINE)
        self.assertEqual(self.resolve(desc="no repo here", project={"id": PROJ}), NO_LINE)
        self.assertEqual(self.run_.calls, [])

    def test_mapped_failures_carry_the_mapping_prefix(self):
        os.makedirs(self.clone)
        r = self.mapped
        want = "ophis/agent-pm: not found or no access (HTTP %s)"
        self.assertEqual(r(api=ok(code=1, stderr="gh: Not Found (HTTP 404)")), eng.Invalid(eng.MAPPED + want % 404))
        self.assertEqual(r(api=ok(code=1, stderr="gh: Forbidden (HTTP 403)")), eng.Invalid(eng.MAPPED + want % 403))
        self.assertEqual(r(api=ok('{"default_branch": "main", "permissions": {"push": false}}')),
                         eng.Invalid(eng.MAPPED + "ophis/agent-pm: no push permission"))
        self.assertEqual(r(api=ok('{"default_branch": "-x", "permissions": {"push": true}}')),
                         eng.Invalid(eng.MAPPED + "ophis/agent-pm: unsafe default branch name"))
        self.assertTrue(eng.MAPPED.startswith("project mapping") and eng.MAPPED.endswith(" "))

    def test_same_failures_from_a_repo_line_have_no_prefix(self):
        os.makedirs(self.clone)
        repos, project = {PROJ: "ophis/agent-pm"}, {"id": PROJ}
        for over, reason in (({"api": ok(code=1, stderr="gh: Not Found (HTTP 404)")}, "ophis/agent-pm: not found or no access (HTTP 404)"),
                             ({"api": ok(code=1, stderr="gh: Forbidden (HTTP 403)")}, "ophis/agent-pm: not found or no access (HTTP 403)"),
                             ({"api": ok('{"default_branch": "main", "permissions": {"push": false}}')}, "ophis/agent-pm: no push permission"),
                             ({"api": ok('{"default_branch": "-x", "permissions": {"push": true}}')}, "ophis/agent-pm: unsafe default branch name")):
            with self.subTest(reason=reason):
                self.assertEqual(self.resolve(repos=repos, project=project, **over), eng.Invalid(reason))

    def test_mapped_other_failures_keep_todays_reason(self):
        os.makedirs(self.clone)
        self.assertEqual(self.mapped(fetch_url=ok("git@github.com:other/agent-pm.git\n")),
                         eng.Invalid(f"{self.clone} is not a clone of ophis/agent-pm (origin URL differs)"))
        self.assertEqual(self.mapped(wt=ok(code=128, stderr="not a git repository")),
                         eng.Invalid(f"{self.clone} exists but is not a git repo"))
        self.assertEqual(self.mapped(branches=ok("TASK-26-a\nTASK-26-b\n")), eng.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"))
        self.assertIsInstance(self.mapped(api=ok(code=1, stderr="gh: Bad Gateway (HTTP 502)")), eng.Transient)

    def test_mapped_clone_outside_playground_keeps_todays_reason(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", other]))
        os.symlink(other, self.clone)
        self.assertEqual(self.mapped(), eng.Invalid(f"{self.clone} is a symlink or outside {self.pg}"))

    def test_mapping_value_that_is_not_a_repo(self):
        for value in ("https://github.com/ophis/agent-pm", "ophis", "", "ophis/..", 42, None):
            with self.subTest(value=value):
                r = self.resolve(desc="no repo here", repos={PROJ: value}, project={"id": PROJ})
                self.assertIsInstance(r, eng.Invalid)
                self.assertTrue(r.reason.startswith(eng.MAPPED), r.reason)
        self.assertEqual(self.run_.calls, [])

class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.root]))
        os.makedirs(os.path.join(self.root, "work"))
        self.wt = os.path.join(self.root, "work", "TASK-26", "worktrees", "TASK-26-x")
        self.ok = eng.Ok("TASK-26", "ENG: X", "ophis", "agent-pm", os.path.join(self.root, "clone"), "main", "TASK-26-x", self.wt)
        patcher = mock.patch.object(eng, "run_dir", lambda issue: os.path.join(self.root, "work", issue))
        patcher.start()
        self.addCleanup(patcher.stop)

    def table(self, **over):
        t = {"show_ref": ok(), "remote": ok(""), "user": ok('{"login": "ophis"}'), "prs": ok(json.dumps([pr_row(7)])),
             "issue_comments": ok("[[]]"), "reviews": ok("[[]]"), "review_comments": ok("[[]]")}
        t.update(over)
        api = ("gh", "api", "--paginate", "--slurp")
        return [(("git", "-C", self.ok.clone, "show-ref"), t["show_ref"]), (("git", "-C", self.ok.clone, "ls-remote"), t["remote"]),
                (("gh", "api", "user"), t["user"]), (("gh", "pr", "list"), t["prs"]),
                ((*api, "repos/ophis/agent-pm/issues/7/comments"), t["issue_comments"]),
                ((*api, "repos/ophis/agent-pm/pulls/7/reviews"), t["reviews"]),
                ((*api, "repos/ophis/agent-pm/pulls/7/comments"), t["review_comments"])]

    def cli(self, *argv, env=None, resolved=None, resolve_error=None, config=None, **over):
        self.run_ = FakeRun(self.table(**over))
        out, err = io.StringIO(), io.StringIO()
        extra = {} if config is None else {"config": config}
        with mock.patch.object(eng, "resolve", return_value=resolved or self.ok, side_effect=resolve_error) as resolve:
            rc = eng.main(list(argv), env={"AGENT_PM_ISSUE": "TASK-26"} if env is None else env,
                          gql=lambda *a, **k: None, run=self.run_, out=out, err=err, **extra)
        self.out, self.err, self.resolve = out.getvalue(), err.getvalue(), resolve
        return rc

    def config_file(self, tail):
        uid = "00000000-0000-4000-8000-000000000000"
        text = f'team = "{uid}"\nharness_key = "k"\n[states]\n' + "".join(f'{k} = "{uid}"\n' for k in pipeline.STATES) + tail
        path = os.path.join(self.root, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_requires_issue_env(self):
        self.assertEqual(self.cli("status", env={}), 2)
        self.assertIn("AGENT_PM_ISSUE", self.err)

    def test_passes_the_configured_mapping_to_resolve(self):
        path = self.config_file(f'[project_repos]\n"{PROJ}" = "ophis/agent-pm"\n')
        self.assertEqual(self.cli("status", config=path), 0)
        self.assertEqual(self.resolve.call_args.kwargs["repos"], {PROJ: "ophis/agent-pm"})
        self.assertEqual(self.cli("status", config=self.config_file("")), 0)
        self.assertEqual(self.resolve.call_args.kwargs["repos"], {})

    def test_default_config_is_the_real_one(self):
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(self.resolve.call_args.kwargs["repos"], pipeline.load_config()["project_repos"])

    def test_broken_config_refused(self):
        self.assertEqual(self.cli("status", config=self.config_file(f'[project_repos]\n"{PROJ}" = "ophis"\n')), 2)
        self.assertTrue(self.err.startswith(f"eng.py: pipeline.toml: project_repos.{PROJ} "), self.err)
        self.assertEqual(self.out, "")
        self.resolve.assert_not_called()

    def test_config_loaded_after_the_issue_check(self):
        self.assertEqual(self.cli("status", env={}, config=os.path.join(self.root, "missing.toml")), 2)
        self.assertIn("AGENT_PM_ISSUE", self.err)

    def test_refuses_when_not_ok(self):
        self.assertEqual(self.cli("status", resolved=eng.Invalid("no Repo")), 2)
        self.assertIn("no Repo", self.err)

    def test_resolve_transient_or_error_exits_3(self):
        self.assertEqual(self.cli("status", resolved=eng.Transient("gh api: TimeoutExpired")), 3)
        self.assertEqual(self.err, "eng.py: transient: gh api: TimeoutExpired\n")
        self.assertEqual(self.cli("comments", "--since", "2026-09-28T00:00:00Z",
                                  resolve_error=subprocess.TimeoutExpired("git", 60)), 3)
        self.assertTrue(self.err.startswith("eng.py: transient:"))
        self.assertEqual(self.cli("status", resolve_error=KeyError("title")), 3)
        self.assertEqual(self.err, "eng.py: transient: resolve: KeyError('title')\n")

    def test_setup_push_pr_commands_removed(self):
        for cmd in ("setup", "push", "pr"):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                eng.main([cmd], env={"AGENT_PM_ISSUE": "TASK-26"}, gql=lambda *a, **k: None,
                          run=FakeRun([]), out=io.StringIO(), err=io.StringIO())
            self.assertEqual(cm.exception.code, 2)

    def test_bad_since_refused(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            self.cli("comments", "--since", "yesterday")
        self.assertEqual(cm.exception.code, 2)

    def test_transient_subprocess_error_exits_3(self):
        self.assertEqual(self.cli("status", show_ref=subprocess.TimeoutExpired("git", 60)), 3)
        self.assertIn("transient", self.err)

    def test_gh_or_git_failure_exits_3(self):
        for cmd, over in (("status", {"prs": ok(code=1, stderr="HTTP 502")}), ("status", {"user": ok(code=1, stderr="auth")}),
                          ("status", {"show_ref": ok(code=128, stderr="fatal")}),
                          ("status", {"show_ref": ok(code=1), "remote": ok(code=128, stderr="no remote")}),
                          ("comments", {"user": ok(code=1, stderr="auth")}), ("comments", {"prs": ok(code=1, stderr="HTTP 502")}),
                          ("comments", {"reviews": ok(code=1, stderr="HTTP 502")})):
            argv = [cmd] + (["--since", "2026-09-28T00:00:00Z"] if cmd == "comments" else [])
            self.assertEqual(self.cli(*argv, **over), 3, over)
            self.assertTrue(self.err.startswith("eng.py: transient:"), self.err)
            self.assertEqual(self.out, "")

    def test_malformed_gh_output_exits_2(self):
        for cmd, over in (("status", {"prs": ok('{"message": "Bad credentials"}')}), ("status", {"prs": ok("not json")}),
                          ("status", {"prs": ok(json.dumps([{**pr_row(7), "number": "7"}]))}),
                          ("status", {"user": ok("[]")}), ("status", {"user": ok('{"login": ""}')}),
                          ("comments", {"issue_comments": ok('{"message": "x"}')}), ("comments", {"issue_comments": ok('[{"a": 1}]')}),
                          ("comments", {"reviews": ok(json.dumps([[{"user": {"login": "ophis"}, "submitted_at": "later"}]]))})):
            argv = [cmd] + (["--since", "2026-09-28T00:00:00Z"] if cmd == "comments" else [])
            self.assertEqual(self.cli(*argv, **over), 2, over)
            self.assertTrue(self.err.startswith("eng.py: malformed gh output:"), self.err)

    def test_comments_keep_only_login(self):
        rows = [{"user": {"login": "ophis"}, "created_at": "2026-09-28T01:00:00Z", "body": "fix a"},
                {"user": {"login": "rando"}, "created_at": "2026-09-28T01:00:00Z", "body": "rm -rf"},
                {"user": {"login": "ophis"}, "created_at": "2026-09-27T01:00:00Z", "body": "old"}]
        reviews = [{"user": {"login": "ophis"}, "submitted_at": "2026-09-28T02:00:00Z", "body": "lgtm but b", "state": "COMMENTED"},
                   {"user": {"login": "ophis"}, "body": "draft", "state": "PENDING"}]
        self.assertEqual(self.cli("comments", "--since", "2026-09-28T00:00:00Z", issue_comments=ok(json.dumps([rows])),
                                  reviews=ok(json.dumps([reviews]))), 0)
        data = json.loads(self.out)
        self.assertEqual([c["body"] for c in data["kept"]], ["fix a", "lgtm but b"])
        self.assertEqual(data["dropped"], 1)

    def test_comments_read_every_page(self):
        page = lambda n: [{"user": {"login": "ophis"}, "created_at": f"2026-09-28T0{n}:00:00Z", "body": f"p{n}"}]
        self.cli("comments", "--since", "2026-09-28T00:00:00Z", issue_comments=ok(json.dumps([page(1), page(2), page(3)])))
        self.assertEqual([c["body"] for c in json.loads(self.out)["kept"]], ["p1", "p2", "p3"])
        self.assertIn((("gh", "api", "--paginate", "--slurp", "repos/ophis/agent-pm/issues/7/comments"), 60), self.run_.calls)

    def test_comments_since_compares_times_not_strings(self):
        rows = [{"user": {"login": "ophis"}, "created_at": "2026-09-28T01:00:00Z", "body": "after"},
                {"user": {"login": "ophis"}, "created_at": "2026-09-27T23:30:00Z", "body": "before"}]
        self.cli("comments", "--since", "2026-09-28T02:00:00+02:00", issue_comments=ok(json.dumps([rows])))
        self.assertEqual([c["body"] for c in json.loads(self.out)["kept"]], ["after"])

    def test_pr_ignores_forks_and_other_authors(self):
        prs = [pr_row(9, fork=True), pr_row(8, login="rando"), pr_row(7)]
        self.assertEqual(self.cli("status", prs=ok(json.dumps(prs))), 0)
        self.assertEqual(json.loads(self.out)["pr"], {"number": 7, "url": "u7", "state": "OPEN"})
        self.assertEqual(self.cli("status", prs=ok(json.dumps(prs[:2]))), 0)
        self.assertIsNone(json.loads(self.out)["pr"])
        self.assertEqual(self.cli("comments", "--since", "2026-09-28T00:00:00Z", prs=ok(json.dumps(prs[:1]))), 0)
        self.assertFalse(any("--paginate" in c[0] for c in self.run_.calls))

    def test_status_json(self):
        os.makedirs(os.path.join(self.wt, "docs", "specs"))
        with open(os.path.join(self.wt, "docs", "specs", "p.md"), "w") as f:
            f.write("x\nRESUME: phase=S5 worktree=...\n")
        self.assertEqual(self.cli("status", prs=ok("[]")), 0)
        s = json.loads(self.out)
        self.assertEqual((s["repo"], s["branch"], s["branch_exists"], s["worktree_exists"], s["pr"]),
                         ("ophis/agent-pm", "TASK-26-x", "local", True, None))
        self.assertEqual(s["plan_docs"], [{"path": os.path.join(self.wt, "docs", "specs", "p.md"), "phase": "S5"}])
        self.assertEqual(s["pr_title"], "TASK-26: X")
        self.assertTrue(s["worktrees_dir_ok"])

    def test_status_skips_unreadable_plan_docs(self):
        os.makedirs(self.wt)
        with open(os.path.join(self.wt, "p.md"), "w") as f:
            f.write("RESUME: phase=S5\n")
        os.symlink(os.path.join(self.root, "gone.md"), os.path.join(self.wt, "broken.md"))
        locked = os.path.join(self.wt, "locked.md")
        open(locked, "w").close()
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(json.loads(self.out)["plan_docs"], [{"path": os.path.join(self.wt, "p.md"), "phase": "S5"}])

    def test_status_pr_title_sanitized(self):
        bad = eng.Ok("TASK-26", 'ENG: a $(rm) `x` "q"', "ophis", "agent-pm", self.ok.clone, "main", "TASK-26-x", self.wt)
        self.assertEqual(self.cli("status", resolved=bad, show_ref=ok(code=1)), 0)
        s = json.loads(self.out)
        self.assertEqual(s["pr_title"], "TASK-26: a (rm) x q")
        self.assertIsNone(s["branch_exists"])

    def test_status_worktrees_dir_ok_false_for_symlink_out(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", outside]))
        run_dir = os.path.join(self.root, "work", "TASK-26")
        os.makedirs(run_dir)
        os.symlink(outside, os.path.join(run_dir, "worktrees"))
        self.assertEqual(self.cli("status"), 0)
        s = json.loads(self.out)
        self.assertFalse(s["worktrees_dir_ok"])

class PrTitle(unittest.TestCase):
    def title(self, t):
        return eng.pr_title(eng.Ok("TASK-38", t, "o", "n", "c", "main", "b", "w"))

    def test_keeps_unicode_letters_and_digits(self):
        self.assertEqual(self.title("ENG: Engineering 骨架"), "TASK-38: Engineering 骨架")
        self.assertEqual(self.title("ENG: 中文标题 2"), "TASK-38: 中文标题 2")

    def test_removes_what_single_quotes_cannot_hold(self):
        self.assertEqual(self.title("ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮"),
                         "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h")
