import dataclasses, io, json, os, sys, subprocess, tempfile, unittest
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER  # noqa: E402
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

ENGINEER, HUMAN = "frank.agent.w+engineer@gmail.com", "Frank@Example.com"
CREATED = "2026-09-01T00:00:00Z"

def lin(at, email, body):
    return {"body": body, "createdAt": at, "user": {"email": email} if email else None}

def gh_row(login, at, body="", key="created_at", **extra):
    return {"user": {"login": login} if login else None, key: at, "body": body, **extra}

def gh_pages(*rows):
    return ok(json.dumps([list(rows)]))

def bodies(entries):
    return [e["body"] for e in entries]

def linear_for(nodes, created=CREATED):
    return lambda q, **v: {"issue": {"createdAt": created, "comments": {"nodes": nodes}}}

class ParseRepo(unittest.TestCase):
    def test_accepted(self):
        for line in ("Repo: ophis/agent-pm", "repo: https://github.com/ophis/agent-pm",
                     "REPO: https://github.com/ophis/agent-pm.git/", "Repo: git@github.com:ophis/agent-pm.git",
                     "repo: [https://github.com/ophis/agent-pm](<https://github.com/ophis/agent-pm>)",
                     "Repo: [agent-pm](https://github.com/ophis/agent-pm)", "  repo:   ophis/agent-pm  ",
                     "`Repo: ophis/agent-pm`", "* Repo: ophis/agent-pm", "- Repo: ophis/agent-pm", "> Repo: ophis/agent-pm",
                     "**Repo:** ophis/agent-pm", "**Repo: ophis/agent-pm**", "Repo: `ophis/agent-pm`"):
            with self.subTest(line=line):
                self.assertEqual(eng.parse_repo(f"x\n{line}\ny"), ("ophis", "agent-pm"))
        for d, want in (("a\r\nRepo: ophis/agent-pm\r\nb", ("ophis", "agent-pm")), ("Repo: ophis/a\nrepo: OPHIS/A", ("ophis", "a")),
                        ("Repo: ophis/.github", ("ophis", ".github")), ("Repo: https://github.com/ophis/.github.git", ("ophis", ".github")),
                        ("Repo: ophis/..x", ("ophis", "..x"))):
            with self.subTest(d=d):
                self.assertEqual(eng.parse_repo(d), want)

    def test_comments_section_ignored(self):
        d = "Repo: ophis/a\n\n## Comments\n* x:\n  > Repo: ophis/b"
        self.assertEqual(eng.parse_repo(d), ("ophis", "a"))
        self.assertIsInstance(eng.parse_repo("## Comments\nRepo: ophis/a"), eng.Invalid)

    def test_rejected(self):
        for d in (*(f"Repo: {v}" for v in ("", "ophis", "-ophis/a", "ophis/-a", "ophis/..", "ophis/.", "a b/c", "ophis/a;rm",
                                          "https://gitlab.com/ophis/a")), "Repo: ophis/a\nRepo: ophis/b"):
            with self.subTest(d=d):
                self.assertIsInstance(eng.parse_repo(d), eng.Invalid)

class Slug(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(eng.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(eng.slug("Engineering 骨架"), "engineering")
        self.assertEqual(eng.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(eng.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(eng.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))

class Resolve(unittest.TestCase):
    MAPPING = {"repos": {PROJ: "ophis/agent-pm"}, "project": {"id": PROJ}}
    ISSUES = (("Repo line", {}, ""), ("Repo line, mapped project", MAPPING, ""), ("mapped project", {"desc": "no repo here", **MAPPING}, eng.MAPPED))

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
            "fetch_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "push_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "branches": ok(""),
            "remote": ok(""),
        }
        t.update(over)
        return [
            (("gh", "api", "repos/ophis/agent-pm"), t["api"]),
            (("gh", "repo", "clone"), t["clone"]),
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

    def test_linear_error_transient(self):
        def boom(q, **v):
            raise SystemExit("linear api error: boom")
        self.assertIsInstance(eng.resolve("TASK-26", boom, FakeRun([]), playground=self.pg), eng.Transient)

    def test_issue_not_found_or_other_identifier_invalid(self):
        self.assertEqual(self.resolve(gql=lambda q, **v: {"issue": None}), eng.Invalid("TASK-26: issue not found"))
        for ident in ("OPS-26", "task-26"):
            self.assertIsInstance(self.resolve(gql=gql_for("Repo: ophis/agent-pm", ident=ident)), eng.Invalid, ident)
            self.assertEqual(self.run_.calls, [])

    def test_worktrees_dir_symlinked_out_invalid(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", outside]))
        base = os.path.join(self.work, "TASK-26")
        os.makedirs(base)
        os.symlink(outside, os.path.join(base, "worktrees"))
        self.assertEqual(self.resolve(), eng.Invalid(f"{os.path.join(base, 'worktrees')} resolves outside {base}"))
        self.assertEqual(self.run_.calls, [])

    def test_access(self):
        os.makedirs(self.clone)
        for res, want in ((ok('{"default_branch": "main", "permissions": {"push": false}}'), eng.Invalid("ophis/agent-pm: no push permission")),
                          (ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"), eng.Transient("gh api repos/ophis/agent-pm: gh: Bad Gateway (HTTP 502)"))):
            with self.subTest(want=want):
                self.assertEqual(self.resolve(api=res), want)

    def test_existing_branches(self):
        os.makedirs(self.clone)
        for over, branch, where in (({"branches": ok("TASK-26-old-name\n")}, "TASK-26-old-name", "local"),
                                    ({"remote": ok("abc\trefs/heads/TASK-26-remote\n")}, "TASK-26-remote", "remote"),
                                    ({}, "TASK-26-session-registry", None)):
            with self.subTest(where=where):
                r = self.resolve(**over)
                self.assertEqual((r.branch, r.branch_exists, r.worktree), (branch, where, os.path.join(self.work, "TASK-26", "worktrees", branch)))
                self.assertEqual(any("ls-remote" in c[0] for c in self.run_.calls), where != "local")
        for over, want in (({"branches": ok("TASK-26-a\nTASK-26-b\n")}, eng.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b")),
                           ({"branches": ok("TASK-26-Bad$(x)\n")}, eng.Invalid("existing branch name 'TASK-26-Bad$(x)' is not TASK-26-<lowercase slug>")),
                           ({"branches": ok(code=128, stderr="fatal")}, eng.Transient("git branch --list: fatal")),
                           ({"remote": ok(code=128, stderr="no remote")}, eng.Transient("git ls-remote: no remote"))):
            with self.subTest(want=want):
                self.assertEqual(self.resolve(**over), want)
                self.assertEqual(self.mapped(**over), want)

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
        for project in ({"id": "other"}, None, {"id": None}, {}, {"id": 7}):
            with self.subTest(project=project):
                self.assertEqual(self.resolve(desc="no repo here", repos={PROJ: "ophis/agent-pm"}, project=project), NO_LINE)
        self.assertEqual(self.resolve(desc="no repo here", project={"id": PROJ}), NO_LINE)
        self.assertEqual(self.run_.calls, [])

    def target(self, desc="Repo: ophis/agent-pm", repos=None, project=None, push=False, **over):
        self.run_ = FakeRun(self.table(**over))
        issue = {"description": desc, "project": project}
        return eng.target(issue, self.run_, playground=self.pg, repos=repos, push=push)

    def test_target_without_push_check_accepts_a_read_only_repo(self):
        os.makedirs(self.clone)
        read_only = ok('{"default_branch": "main", "permissions": {"push": false}}')
        self.assertEqual(self.target(api=read_only), eng.Target("ophis", "agent-pm", self.clone, "main", False))
        for issue, extra, prefix in self.ISSUES:
            with self.subTest(issue=issue):
                self.assertEqual(self.target(api=read_only, push=True, **extra), eng.Invalid(prefix + "ophis/agent-pm: no push permission"))

    def test_target_is_not_the_branch_step_and_ignores_the_url_case(self):
        os.makedirs(self.clone)
        self.assertEqual(self.target(fetch_url=ok("https://github.com/OPHIS/Agent-PM\n")), eng.Target("ophis", "agent-pm", self.clone, "main", False))
        self.assertFalse(any(c[0][3] in ("branch", "ls-remote") for c in self.run_.calls if c[0][:2] == ("git", "-C")))

    def test_target_keeps_every_other_check_with_push_off(self):
        os.makedirs(self.clone)
        self.assertEqual(self.target(desc="no repo here"), NO_LINE)
        self.assertEqual(self.target(desc="Repo: nope"), eng.Invalid("unreadable Repo line: 'nope'"))
        default = lambda name: ok(json.dumps({"default_branch": name, "permissions": {"push": False}}))
        unsafe = eng.Invalid("ophis/agent-pm: unsafe default branch name")
        for label, over, want, tagged in (
                ("HTTP 404", {"api": ok(code=1, stderr="gh: Not Found (HTTP 404)")}, eng.Invalid("ophis/agent-pm: not found or no access (HTTP 404)"), True),
                ("HTTP 403", {"api": ok(code=1, stderr="gh: Forbidden (HTTP 403)")}, eng.Invalid("ophis/agent-pm: not found or no access (HTTP 403)"), True),
                ("HTTP 502", {"api": ok(code=1, stderr="gh: Bad Gateway (HTTP 502)")},
                 eng.Transient("gh api repos/ophis/agent-pm: gh: Bad Gateway (HTTP 502)"), False),
                ("no HTTP code", {"api": ok(code=1, stderr="error connecting to api.github.com")},
                 eng.Transient("gh api repos/ophis/agent-pm: error connecting to api.github.com"), False),
                ("default -x", {"api": default("-x")}, unsafe, True),
                ("default a..b", {"api": default("a..b")}, unsafe, True),
                ("default ma$(x)", {"api": default("ma$(x)")}, unsafe, True),
                ("default null", {"api": default(None)}, unsafe, True),
                ("origin URL", {"fetch_url": ok("git@github.com:other/agent-pm.git\n")},
                 eng.Invalid(f"{self.clone} is not a clone of ophis/agent-pm (origin URL differs)"), False),
                ("origin push URL", {"push_url": ok("https://evil.example/x.git\n")},
                 eng.Invalid(f"{self.clone} is not a clone of ophis/agent-pm (origin push URL differs)"), False)):
            for issue, extra, prefix in self.ISSUES:
                with self.subTest(label, issue=issue):
                    self.assertEqual(self.target(**extra, **over), eng.Invalid(prefix + want.reason) if tagged else want)
        self.assertEqual(eng.MAPPED, "project mapping ")

    def test_target_clones_a_missing_clone_and_reports_a_failure(self):
        self.assertEqual(self.target(), eng.Target("ophis", "agent-pm", self.clone, "main", False))
        self.assertIn((("gh", "repo", "clone", "ophis/agent-pm", self.clone), 600), self.run_.calls)
        self.assertIsInstance(self.target(clone=ok(code=1, stderr="network")), eng.Transient)

    def test_target_clone_symlink_invalid(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", other]))
        os.symlink(other, self.clone)
        self.assertEqual(self.target(), eng.Invalid(f"{self.clone} is a symlink or outside {self.pg}"))
        self.assertEqual(self.target("no repo here", **self.MAPPING), eng.Invalid(f"{self.clone} is a symlink or outside {self.pg}"))

    def test_target_mapping(self):
        os.makedirs(self.clone)
        repos, project = {PROJ: "ophis/agent-pm"}, {"id": PROJ}
        self.assertEqual(self.target("no repo here", repos=repos, project=project),
                         eng.Target("ophis", "agent-pm", self.clone, "main", True))
        self.assertEqual(self.target("Repo: ophis/agent-pm", repos={PROJ: "ophis/other"}, project=project),
                         eng.Target("ophis", "agent-pm", self.clone, "main", False))
        self.assertEqual(self.target("no repo here", repos=repos, project=project, api=ok(code=1, stderr="gh: Not Found (HTTP 404)")),
                         eng.Invalid(eng.MAPPED + "ophis/agent-pm: not found or no access (HTTP 404)"))
        self.assertEqual(self.target("no repo here", repos={PROJ: "nope"}, project=project),
                         eng.Invalid(f"{eng.MAPPED}'nope': not <owner>/<name>"))
        self.assertEqual(self.target("no repo here", repos={PROJ: 42}, project=project), eng.Invalid(f"{eng.MAPPED}'42': not <owner>/<name>"))
        self.assertEqual(self.run_.calls, [])
        self.assertEqual(self.target("no repo here", repos=repos, project={"id": "other"}), NO_LINE)

class RunSubdir(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.work]))
        patcher = mock.patch.object(pipeline, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = os.path.join(self.work, "TASK-26")

    def test_path_inside_the_run_dir(self):
        self.assertEqual(eng.run_subdir("TASK-26", "src"), os.path.join(self.base, "src"))
        os.makedirs(os.path.join(self.base, "src"))
        self.assertEqual(eng.run_subdir("TASK-26", "src"), os.path.join(self.base, "src"))

    def test_symlink_inside_the_run_dir_is_fine(self):
        os.makedirs(os.path.join(self.base, "real"))
        os.symlink(os.path.join(self.base, "real"), os.path.join(self.base, "src"))
        self.assertEqual(eng.run_subdir("TASK-26", "src"), os.path.join(self.base, "src"))

class Locate(unittest.TestCase):
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.root]))
        self.clone = os.path.join(self.root, "clone")

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def wt(self, head, name="wt", gitdir=None):
        path = os.path.join(self.root, name)
        gitdir = gitdir or os.path.join(self.clone, ".git", "worktrees", name)
        self.put(os.path.join(path, ".git"), f"gitdir: {gitdir}\n")
        self.put(os.path.join(gitdir, "HEAD"), head)
        return path

    def test_located(self):
        relative = self.wt(self.SHA + "\n", name="relative")
        self.put(os.path.join(relative, ".git"), "gitdir: ../clone/.git/worktrees/relative\n")
        no_head = self.wt("", name="no-head")
        os.remove(os.path.join(self.clone, ".git", "worktrees", "no-head", "HEAD"))
        for path, branch, head in ((self.wt(self.SHA + "\n"), None, self.SHA),
                                   (self.wt("ref: refs/heads/TASK-1-x\n", name="branch"), "TASK-1-x", "ref: refs/heads/TASK-1-x"),
                                   (relative, None, self.SHA), (no_head, None, "")):
            with self.subTest(path=path):
                gitdir = os.path.join(self.clone, ".git", "worktrees", os.path.basename(path))
                self.assertEqual(eng.locate(path), (self.clone, branch, head, gitdir))

    def test_refusals(self):
        self.put(os.path.join(self.root, "dir", ".git", "x"), "")
        self.put(os.path.join(self.root, "text", ".git"), "not a gitdir\n")
        self.put(os.path.join(self.root, "elsewhere", ".git"), f"gitdir: {self.root}/elsewhere/x\n")
        bare = os.path.join(self.root, "clone", "worktrees", "w")
        self.put(os.path.join(bare, "HEAD"), self.SHA)
        self.put(os.path.join(self.root, "bare", ".git"), f"gitdir: {bare}\n")
        for path, reason in (("missing", "clone unknown: .git is not a gitdir file"), ("dir", "clone unknown: .git is not a gitdir file"),
                             ("text", "clone unknown: .git is not a gitdir file"),
                             ("elsewhere", "clone unknown: gitdir is not <clone>/.git/worktrees/<name>"),
                             ("bare", "clone unknown: gitdir is not <clone>/.git/worktrees/<name>")):
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as cm:
                    eng.locate(os.path.join(self.root, path))
                self.assertEqual(str(cm.exception), reason)
        with self.assertRaises(ValueError) as cm:
            eng.locate(self.wt("ref: refs/heads/-D\n", name="bad"))
        self.assertEqual(str(cm.exception), "unsafe branch name")

class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.root]))
        self.wt = os.path.join(self.root, "work", "TASK-26", "worktrees", "TASK-26-x")
        self.ok = eng.Ok("TASK-26", "ENG: X", "ophis", "agent-pm", os.path.join(self.root, "clone"), "main", "TASK-26-x", self.wt,
                         branch_exists="local")

    def table(self, **over):
        t = {"user": ok('{"login": "ophis"}'), "prs": ok(json.dumps([pr_row(7)])),
             "issue_comments": ok("[[]]"), "reviews": ok("[[]]"), "review_comments": ok("[[]]")}
        t.update(over)
        api = ("gh", "api", "--paginate", "--slurp")
        return [(("gh", "api", "user"), t["user"]), (("gh", "pr", "list"), t["prs"]),
                ((*api, "repos/ophis/agent-pm/issues/7/comments"), t["issue_comments"]),
                ((*api, "repos/ophis/agent-pm/pulls/7/reviews"), t["reviews"]),
                ((*api, "repos/ophis/agent-pm/pulls/7/comments"), t["review_comments"])]

    def cli(self, *argv, env=None, resolved=None, resolve_error=None, config=None, gql=None, **over):
        self.run_ = FakeRun(self.table(**over))
        out, err = io.StringIO(), io.StringIO()
        extra = {} if config is None else {"config": config}
        with mock.patch.object(eng, "resolve", return_value=resolved or self.ok, side_effect=resolve_error) as resolve:
            rc = eng.main(list(argv), env={"AGENT_PM_ISSUE": "TASK-26"} if env is None else env,
                          gql=gql or linear_for([]), run=self.run_, out=out, err=err, **extra)
        self.out, self.err, self.resolve = out.getvalue(), err.getvalue(), resolve
        return rc

    def comments(self, nodes=(), created=CREATED, **over):
        """Runs `comments` against fake Linear comments and the humans [HUMAN]; returns the parsed output."""
        config = self.config_file(f'human_members = ["{HUMAN}"]\n')
        self.assertEqual(self.cli("comments", config=config, gql=linear_for(list(nodes), created), **over), 0, self.err)
        return json.loads(self.out)

    def config_file(self, tail):
        path = os.path.join(self.root, "pipeline.toml")
        with open(path, "w") as f:
            f.write(HEADER + tail)
        return path

    def test_requires_issue_env_before_the_config(self):
        for config in (None, os.path.join(self.root, "missing.toml")):
            with self.subTest(config=config):
                self.assertEqual(self.cli("status", env={}, config=config), 1)
                self.assertEqual(self.err, "eng.py: AGENT_PM_ISSUE is not set to an issue id\n")

    def test_passes_the_configured_mapping_to_resolve(self):
        path = self.config_file(f'[project_repos]\n"{PROJ}" = "ophis/agent-pm"\n')
        self.assertEqual(self.cli("status", config=path), 0)
        self.assertEqual(self.resolve.call_args.kwargs["repos"], {PROJ: "ophis/agent-pm"})
        self.assertEqual(self.cli("status", config=self.config_file("")), 0)
        self.assertEqual(self.resolve.call_args.kwargs["repos"], {})

    def test_broken_config_refused(self):
        self.assertEqual(self.cli("status", config=self.config_file(f'[project_repos]\n"{PROJ}" = "ophis"\n')), 1)
        self.assertTrue(self.err.startswith(f"eng.py: pipeline.toml: project_repos.{PROJ} "), self.err)
        self.assertEqual(self.out, "")
        self.resolve.assert_not_called()

    def test_resolve_failure_exits_1(self):
        for cmd, kw, err in (("status", {"resolved": eng.Invalid("no Repo")}, "no Repo"),
                             ("status", {"resolved": eng.Transient("gh api: TimeoutExpired")}, "gh api: TimeoutExpired"),
                             ("comments", {"resolve_error": subprocess.TimeoutExpired("git", 60)}, "resolve: TimeoutExpired('git', 60)"),
                             ("status", {"resolve_error": KeyError("title")}, "resolve: KeyError('title')")):
            with self.subTest(err=err):
                self.assertEqual(self.cli(cmd, **kw), 1)
                self.assertEqual((self.out, self.err), ("", f"eng.py: {err}\n"))

    def test_gh_failure_or_bad_output_exits_1(self):
        timeout = subprocess.TimeoutExpired("git", 60)
        pr_list = "gh pr list --repo ophis/agent-pm --head TASK-26-x --state all --json number,url,state,isCrossRepository,author --limit 100"
        reviews = "repos/ophis/agent-pm/pulls/7/reviews"
        for cmd, over, err in (("status", {"user": timeout}, str(timeout)),
                               ("status", {"prs": ok(code=1, stderr="HTTP 502")}, f"{pr_list}: HTTP 502"),
                               ("status", {"user": ok(code=1, stderr="auth")}, "gh api user: auth"),
                               ("comments", {"user": ok(code=1, stderr="auth")}, "gh api user: auth"),
                               ("comments", {"prs": ok(code=1, stderr="HTTP 502")}, f"{pr_list}: HTTP 502"),
                               ("comments", {"reviews": ok(code=1, stderr="HTTP 502")}, f"gh api --paginate --slurp {reviews}: HTTP 502"),
                               ("status", {"prs": ok('{"message": "Bad credentials"}')}, "gh pr list: not a JSON list of objects"),
                               ("status", {"prs": ok("not json")}, f"{pr_list}: Expecting value: line 1 column 1 (char 0)"),
                               ("status", {"prs": ok(json.dumps([{**pr_row(7), "number": "7"}]))}, "gh pr list: a PR without a number"),
                               ("status", {"user": ok("[]")}, "gh api user: no login"),
                               ("status", {"user": ok('{"login": ""}')}, "gh api user: no login"),
                               ("comments", {"issue_comments": ok('{"message": "x"}')}, "repos/ophis/agent-pm/issues/7/comments: not a JSON array of pages"),
                               ("comments", {"issue_comments": ok('[{"a": 1}]')}, "repos/ophis/agent-pm/issues/7/comments: not a JSON array of pages"),
                               ("comments", {"reviews": ok(json.dumps([[{"user": {"login": "ophis"}, "submitted_at": "later"}]]))},
                                f"{reviews}: bad submitted_at 'later'")):
            with self.subTest(cmd=cmd, over=over):
                self.assertEqual(self.cli(cmd, **over), 1)
                self.assertEqual((self.out, self.err), ("", f"eng.py: {err}\n"))

    def test_since_is_the_latest_engineer_build_started(self):
        d = self.comments([lin("2026-09-10T00:00:00Z", ENGINEER, "Build started"), lin("2026-09-12T00:00:00Z", HUMAN.lower(), "old ask"),
                           lin("2026-09-20T12:00:00Z", ENGINEER.upper(), "  Build started\n\nrework"),
                           lin("2026-09-15T00:00:00Z", ENGINEER, "Build started"), lin("2026-09-21T00:00:00Z", HUMAN.lower(), "new ask"),
                           lin("2026-09-21T01:00:00Z", HUMAN.lower(), "Build started")],
                          issue_comments=gh_pages(gh_row("ophis", "2026-09-19T00:00:00Z", "old pr"), gh_row("ophis", "2026-09-22T00:00:00Z", "new pr")),
                          reviews=gh_pages(gh_row("rando", "2026-09-19T00:00:00Z", "old review", key="submitted_at", state="COMMENTED"),
                                           gh_row("rando", "2026-09-23T00:00:00Z", "new review", key="submitted_at", state="COMMENTED")))
        self.assertEqual(d["since"], "2026-09-20T12:00:00+00:00")
        self.assertEqual(bodies(d["user"]), ["new ask", "Build started", "new pr"])
        self.assertEqual(bodies(d["others"]), ["new review"])

    def test_no_build_started_since_is_the_issue_creation(self):
        d = self.comments([lin("2026-09-02T00:00:00Z", HUMAN.lower(), "ask")], created="2026-09-01T00:00:00Z",
                          issue_comments=gh_pages(gh_row("ophis", "2026-08-31T23:59:59Z", "before"), gh_row("ophis", "2026-09-03T00:00:00Z", "after")))
        self.assertEqual(d["since"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(bodies(d["user"]), ["ask", "after"])

    def test_entries_at_since_excluded_and_times_compared_as_instants(self):
        d = self.comments([lin("2026-09-20T12:00:00Z", ENGINEER, "Build started"), lin("2026-09-20T12:00:00Z", HUMAN.lower(), "at since")],
                          issue_comments=gh_pages(gh_row("ophis", "2026-09-20T14:00:00+02:00", "same instant"),
                                                  gh_row("ophis", "2026-09-20T13:59:00+02:00", "earlier"),
                                                  gh_row("ophis", "2026-09-20T14:01:00+02:00", "later"),
                                                  gh_row("ophis", "2026-09-20T12:00:01Z", "one second later")))
        self.assertEqual(bodies(d["user"]), ["one second later", "later"])

    def test_pr_kinds_carry_state_path_and_line(self):
        d = self.comments([lin("2026-09-02T00:00:00Z", HUMAN.lower(), "ask")],
                          issue_comments=gh_pages(gh_row("ophis", "2026-09-03T00:00:00Z", "talk")),
                          reviews=gh_pages(gh_row("ophis", "2026-09-04T00:00:00Z", "verdict", key="submitted_at", state="CHANGES_REQUESTED")),
                          review_comments=gh_pages(gh_row("ophis", "2026-09-05T00:00:00Z", "inline", path="a.py", line=5, original_line=3),
                                                   gh_row("ophis", "2026-09-06T00:00:00Z", "outdated", path="b.py", line=None, original_line=9)))
        self.assertEqual(d["user"], [
            {"at": "2026-09-02T00:00:00Z", "source": "linear", "kind": "comment", "author": HUMAN.lower(), "body": "ask"},
            {"at": "2026-09-03T00:00:00Z", "source": "pr", "kind": "comment", "author": "ophis", "body": "talk"},
            {"at": "2026-09-04T00:00:00Z", "source": "pr", "kind": "review", "author": "ophis", "body": "verdict", "state": "CHANGES_REQUESTED"},
            {"at": "2026-09-05T00:00:00Z", "source": "pr", "kind": "review_comment", "author": "ophis", "body": "inline", "path": "a.py", "line": 5},
            {"at": "2026-09-06T00:00:00Z", "source": "pr", "kind": "review_comment", "author": "ophis", "body": "outdated", "path": "b.py", "line": 9}])

    def test_other_authors_are_apart_and_linear_non_humans_in_neither(self):
        d = self.comments([lin("2026-09-02T00:00:00Z", ENGINEER, "agent note"), lin("2026-09-03T00:00:00Z", "bot@linear.app", "integration"),
                           lin("2026-09-04T00:00:00Z", None, "deleted"), lin("2026-09-05T00:00:00Z", HUMAN.lower(), "ask")],
                          issue_comments=gh_pages(gh_row("ophis", "2026-09-06T00:00:00Z", "mine"), gh_row("copilot", "2026-09-07T00:00:00Z", "bot said")),
                          reviews=gh_pages(gh_row("rando", "2026-09-08T00:00:00Z", "nit", key="submitted_at", state="COMMENTED")),
                          review_comments=gh_pages(gh_row(None, "2026-09-09T00:00:00Z", "ghost", path="c.py", line=1)))
        self.assertEqual(bodies(d["user"]), ["ask", "mine"])
        self.assertEqual([(e["kind"], e["author"], e["body"]) for e in d["others"]],
                         [("comment", "copilot", "bot said"), ("review", "rando", "nit"), ("review_comment", None, "ghost")])
        self.assertEqual({e["source"] for e in d["others"]}, {"pr"})

    def test_user_entries_are_one_list_oldest_first(self):
        d = self.comments([lin("2026-09-05T00:00:00Z", HUMAN.lower(), "linear late"), lin("2026-09-02T00:00:00Z", HUMAN.lower(), "linear early")],
                          issue_comments=gh_pages(gh_row("ophis", "2026-09-04T00:00:00Z", "pr mid")),
                          review_comments=gh_pages(gh_row("ophis", "2026-09-03T00:00:00Z", "pr early", path="a", line=1)))
        self.assertEqual(bodies(d["user"]), ["linear early", "pr early", "pr mid", "linear late"])

    def test_humans_match_case_insensitively(self):
        d = self.comments([lin("2026-09-02T00:00:00Z", HUMAN.upper(), "ask")])
        self.assertEqual(bodies(d["user"]), ["ask"])

    def test_every_gh_page_is_read(self):
        page = lambda n: [gh_row("ophis", f"2026-09-0{n}T00:00:00Z", f"p{n}")]
        d = self.comments(issue_comments=ok(json.dumps([page(2), page(3), page(4)])))
        self.assertEqual(bodies(d["user"]), ["p2", "p3", "p4"])
        self.assertIn((("gh", "api", "--paginate", "--slurp", "repos/ophis/agent-pm/issues/7/comments"), 60), self.run_.calls)

    def test_pending_review_skipped(self):
        d = self.comments(reviews=gh_pages(gh_row("ophis", None, "draft", key="submitted_at", state="PENDING"),
                                           gh_row("ophis", "2026-09-03T00:00:00Z", "sent", key="submitted_at", state="COMMENTED")))
        self.assertEqual(bodies(d["user"]), ["sent"])

    def test_no_pr_reads_linear_only(self):
        d = self.comments([lin("2026-09-02T00:00:00Z", HUMAN.lower(), "ask")], prs=ok("[]"))
        self.assertEqual(bodies(d["user"]), ["ask"])
        self.assertEqual(d["others"], [])
        self.assertFalse(any("--paginate" in c[0] for c in self.run_.calls))

    def test_pr_ignores_forks_and_other_authors(self):
        prs = [pr_row(9, fork=True), pr_row(8, login="rando"), pr_row(7)]
        self.assertEqual(self.cli("status", prs=ok(json.dumps(prs))), 0)
        self.assertEqual(json.loads(self.out)["pr"], {"number": 7, "url": "u7", "state": "OPEN"})
        self.assertEqual(self.cli("status", prs=ok(json.dumps(prs[:2]))), 0)
        self.assertIsNone(json.loads(self.out)["pr"])

    def test_status_json(self):
        os.makedirs(os.path.join(self.wt, "docs", "specs"))
        with open(os.path.join(self.wt, "docs", "specs", "p.md"), "w") as f:
            f.write("x\nRESUME: phase=S5 branch=TASK-26-x worktree=...\n")
        self.assertEqual(self.cli("status", prs=ok("[]")), 0)
        self.assertEqual(json.loads(self.out), {
            "repo": "ophis/agent-pm", "clone": self.ok.clone, "default": "main", "branch": "TASK-26-x", "worktree": self.wt,
            "branch_exists": "local", "worktree_exists": True, "pr": None, "pr_title": "TASK-26: X",
            "plan_docs": [{"path": os.path.join(self.wt, "docs", "specs", "p.md"), "phase": "S5"}]})
        self.assertEqual(self.cli("status", resolved=dataclasses.replace(self.ok, branch_exists=None)), 0)
        self.assertIsNone(json.loads(self.out)["branch_exists"])

    def test_status_plan_docs_only_this_branch(self):
        os.makedirs(self.wt)
        for name, line in (("other.md", "phase=S9 branch=TASK-5-y"), ("prefix.md", "phase=S4 branch=TASK-26-xy"),
                           ("bare.md", "phase=S2 worktree=...")):
            with open(os.path.join(self.wt, name), "w") as f:
                f.write(f"x\nRESUME: {line}\n")
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(json.loads(self.out)["plan_docs"], [])
        with open(os.path.join(self.wt, "mine.md"), "w") as f:
            f.write("x\nRESUME: phase=S3 worktree=... branch=TASK-26-x\n")
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(json.loads(self.out)["plan_docs"], [{"path": os.path.join(self.wt, "mine.md"), "phase": "S3"}])

    def test_status_skips_unreadable_plan_docs(self):
        os.makedirs(self.wt)
        with open(os.path.join(self.wt, "p.md"), "w") as f:
            f.write("RESUME: phase=S5 branch=TASK-26-x\n")
        os.symlink(os.path.join(self.root, "gone.md"), os.path.join(self.wt, "broken.md"))
        locked = os.path.join(self.wt, "locked.md")
        open(locked, "w").close()
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(json.loads(self.out)["plan_docs"], [{"path": os.path.join(self.wt, "p.md"), "phase": "S5"}])

class PrTitle(unittest.TestCase):
    def title(self, t):
        return eng.pr_title(eng.Ok("TASK-38", t, "o", "n", "c", "main", "b", "w"))

    def test_keeps_unicode_letters_and_digits(self):
        self.assertEqual(self.title("ENG: Engineering 骨架"), "TASK-38: Engineering 骨架")
        self.assertEqual(self.title("ENG: 中文标题 2"), "TASK-38: 中文标题 2")

    def test_removes_what_single_quotes_cannot_hold(self):
        self.assertEqual(self.title("ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮"),
                         "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h")
