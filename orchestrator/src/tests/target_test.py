import json, os, shutil, subprocess, sys, tempfile, unittest
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import issues  # noqa: E402
import config  # noqa: E402
import repo  # noqa: E402
import target  # noqa: E402

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
NO_LINE = target.Invalid("no Repo: line in the description or its ## Instructions")
LOCAL = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C")

def issue(description, title="ENG: Session Registry", ident="TASK-26", project=None):
    return issues.Issue(id="uuid-26", identifier=ident, url=f"https://linear.app/t/issue/{ident}", title=title,
                        description=description, created_at="2026-09-01T00:00:00.000Z", project_id=project,
                        notes=(), links=(), linked=())

def api(full_name="ophis/agent-pm", default="main", push=True):
    return ok(json.dumps({"full_name": full_name, "default_branch": default, "permissions": {"push": push}}))

class ParseRepo(unittest.TestCase):
    def test_accepted(self):
        for line in ("Repo: ophis/agent-pm", "repo: https://github.com/ophis/agent-pm",
                     "REPO: https://github.com/ophis/agent-pm.git/", "Repo: git@github.com:ophis/agent-pm.git",
                     "repo: [https://github.com/ophis/agent-pm](<https://github.com/ophis/agent-pm>)",
                     "Repo: [agent-pm](https://github.com/ophis/agent-pm)", "  repo:   ophis/agent-pm  ",
                     "`Repo: ophis/agent-pm`", "* Repo: ophis/agent-pm", "- Repo: ophis/agent-pm", "> Repo: ophis/agent-pm",
                     "**Repo:** ophis/agent-pm", "**Repo: ophis/agent-pm**", "Repo: `ophis/agent-pm`"):
            with self.subTest(line=line):
                self.assertEqual(target.parse_repo(f"x\n{line}\ny"), ("ophis", "agent-pm"))
        for d, want in (("a\r\nRepo: ophis/agent-pm\r\nb", ("ophis", "agent-pm")), ("Repo: ophis/a\nrepo: OPHIS/A", ("ophis", "a")),
                        ("Repo: ophis/.github", ("ophis", ".github")), ("Repo: https://github.com/ophis/.github.git", ("ophis", ".github")),
                        ("Repo: ophis/..x", ("ophis", "..x"))):
            with self.subTest(d=d):
                self.assertEqual(target.parse_repo(d), want)

    def test_comments_section_ignored(self):
        d = "Repo: ophis/a\n\n## Comments\n* x:\n  > Repo: ophis/b"
        self.assertEqual(target.parse_repo(d), ("ophis", "a"))
        self.assertIsInstance(target.parse_repo("## Comments\nRepo: ophis/a"), target.Invalid)

    def test_rejected(self):
        for d in (*(f"Repo: {v}" for v in ("", "ophis", "-ophis/a", "ophis/-a", "ophis/..", "ophis/.", "a b/c", "ophis/a;rm",
                                          "https://gitlab.com/ophis/a")), "Repo: ophis/a\nRepo: ophis/b"):
            with self.subTest(d=d):
                self.assertIsInstance(target.parse_repo(d), target.Invalid)

    def test_no_line_and_messages(self):
        self.assertEqual(target.NO_LINE, NO_LINE)
        self.assertEqual(target.parse_repo(None), NO_LINE)
        self.assertEqual(target.parse_repo("Repo: nope"), target.Invalid("unreadable Repo line: 'nope'"))
        self.assertEqual(target.parse_repo("Repo: ophis/a\nRepo: ophis/b"), target.Invalid("several different Repo: values"))

class Slug(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(target.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(target.slug("Engineering 骨架"), "engineering")
        self.assertEqual(target.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(target.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(target.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))

class PrTitle(unittest.TestCase):
    def test_keeps_unicode_letters_and_digits(self):
        self.assertEqual(target.pr_title("TASK-38", "ENG: Engineering 骨架"), "TASK-38: Engineering 骨架")
        self.assertEqual(target.pr_title("TASK-38", "ENG: 中文标题 2"), "TASK-38: 中文标题 2")

    def test_removes_what_single_quotes_cannot_hold(self):
        self.assertEqual(target.pr_title("TASK-38", "ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮"),
                         "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h")

class ResearchRepo(unittest.TestCase):
    REPOS = {PROJ: "ophis/agent-pm"}

    def test_mapping_when_no_repo_line(self):
        self.assertEqual(target.research_repo(issue("no repo here", project=PROJ), self.REPOS),
                         target.Target("ophis", "agent-pm"))
        self.assertEqual(target.research_repo(issue("", project=PROJ), self.REPOS), target.Target("ophis", "agent-pm"))

    def test_repo_line_wins(self):
        self.assertIsNone(target.research_repo(issue("Repo: ophis/other", project=PROJ), self.REPOS))

    def test_a_bad_repo_line_is_not_replaced_by_the_mapping(self):
        for d in ("Repo: nope", "Repo: ophis/a\nRepo: ophis/b"):
            with self.subTest(d=d):
                self.assertIsNone(target.research_repo(issue(d, project=PROJ), self.REPOS))

    def test_none_without_a_mapped_project(self):
        for project in ("other", None):
            with self.subTest(project=project):
                self.assertIsNone(target.research_repo(issue("no repo here", project=project), self.REPOS))
        self.assertIsNone(target.research_repo(issue("no repo here", project=PROJ), {}))

class Check(unittest.TestCase):
    REPOS = {PROJ: "ophis/agent-pm"}
    MAPPED = {"desc": "no repo here", "repos": REPOS, "project": PROJ}
    ISSUES = (("Repo line", {}, ""), ("Repo line, mapped project", {"repos": REPOS, "project": PROJ}, ""), ("mapped project", MAPPED, target.MAPPED))

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.clone = os.path.join(self.work, "TASK-26", config.CLONES[0], "ophis", "agent-pm")
        self.ls_remote = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                          "ls-remote", "--heads", "https://github.com/ophis/agent-pm.git", "TASK-26-*"]

    def git_dir(self):
        os.makedirs(os.path.join(self.clone, ".git"))

    def git_file(self):
        os.makedirs(self.clone)
        with open(os.path.join(self.clone, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/agent-pm\n")

    def branch_list(self, at=None):
        at = at or self.clone
        return (*LOCAL, at, f"--git-dir={at}/.git", "branch", "--list", "TASK-26-*", "--format=%(refname:short)")

    def check(self, desc="Repo: ophis/agent-pm", repos=None, project=None, title="ENG: Session Registry", **over):
        t = {"api": api(), "local": ok(""), "remote": ok("")}
        t.update(over)
        self.run_ = FakeRun([(("gh", "api"), t["api"]), (LOCAL, t["local"]), (("git", "-c", "credential.helper="), t["remote"])])
        return target.check(issue(desc, title=title, project=project), repos or {}, run=self.run_, work=self.work)

    def argvs(self):
        return [c[0] for c in self.run_.calls]

    def test_constants(self):
        self.assertEqual(target.MAPPED, "project mapping ")
        self.assertEqual(target.Target("o", "n"), target.Target("o", "n", ""))
        self.assertEqual(target.Target("o", "n").clone, "")

    def test_ok_without_a_local_clone_asks_the_remote(self):
        self.assertEqual(self.check(), target.Target("ophis", "agent-pm", "TASK-26-session-registry"))
        self.assertEqual(self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), repo.SHORT), (tuple(self.ls_remote), repo.SHORT)])

    def test_local_clone_branches_come_first(self):
        self.git_dir()
        r = self.check(local=ok("TASK-26-old-name\n"))
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-old-name"))
        self.assertEqual(self.run_.calls, [
            (("gh", "api", "repos/ophis/agent-pm"), repo.SHORT),
            (self.branch_list(), repo.SHORT)])

    def test_local_clone_without_branches_falls_back_to_the_remote(self):
        self.git_dir()
        r = self.check(remote=ok("abc\trefs/heads/TASK-26-remote\n"))
        self.assertEqual(r.branch, "TASK-26-remote")
        self.assertEqual(self.argvs()[1:], [self.branch_list(), tuple(self.ls_remote)])

    def test_remote_names_are_taken_after_refs_heads(self):
        r = self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/tags/x\n"))
        self.assertEqual(r.branch, "TASK-26-a")
        self.assertEqual(self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/heads/TASK-26-b\n")),
                         target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"))

    def test_a_worktrees_branches_are_read_on_github(self):
        self.git_file()
        r = self.check(local=ok("TASK-26-planted\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
        self.assertEqual(r.branch, "TASK-26-remote")
        self.assertEqual(self.argvs()[1:], [tuple(self.ls_remote)])

    def test_symlinked_git_is_not_run(self):
        os.makedirs(self.clone)
        elsewhere = os.path.join(self.work, "elsewhere")
        os.makedirs(os.path.join(elsewhere, ".git"))
        for target_ in (os.path.join(elsewhere, ".git"), os.path.join(elsewhere, "gone")):
            git = os.path.join(self.clone, ".git")
            os.symlink(target_, git)
            with self.subTest(target_):
                r = self.check(local=ok("TASK-26-planted\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
                self.assertEqual(r.branch, "TASK-26-remote")
                self.assertEqual(self.argvs()[1:], [tuple(self.ls_remote)])
            os.unlink(git)

    def test_legacy_src_name_path_is_not_read(self):
        os.makedirs(os.path.join(self.work, "TASK-26", config.CLONES[0], "agent-pm", ".git"))
        r = self.check(local=ok("TASK-26-old\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
        self.assertEqual(r.branch, "TASK-26-remote")
        self.assertEqual(self.argvs()[1:], [tuple(self.ls_remote)])

    def test_read_only_task_branches_are_not_the_engineering_branch(self):
        read_only = [f"TASK-26-{t}" for t, spec in config.TASKS.items() if spec.kind != "build"]
        self.assertEqual(sorted(read_only), ["TASK-26-deep-research", "TASK-26-light-research", "TASK-26-product-design"])
        self.git_dir()
        r = self.check(local=ok("\n".join(["TASK-26-session-registry", *read_only]) + "\n"))
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-session-registry"))
        self.assertEqual(self.argvs()[1:], [self.branch_list()])
        r = self.check(local=ok("\n".join(read_only) + "\n"), remote=ok(""))
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-session-registry"))
        self.assertEqual(self.argvs()[1:], [self.branch_list(), tuple(self.ls_remote)])

    def test_read_only_branch_alone_falls_back_to_the_remote(self):
        self.git_dir()
        r = self.check(local=ok("TASK-26-light-research\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
        self.assertEqual(r.branch, "TASK-26-remote")

    def test_only_the_exact_read_only_names_are_dropped(self):
        self.git_dir()
        r = self.check(local=ok("TASK-26-light-research-2\nTASK-26-light-research\n"))
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-light-research-2"))
        r = self.check(local=ok("TASK-26-engineering\nTASK-26-light-research\n"))
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-engineering"))

    def test_no_clone_dir_runs_no_local_git(self):
        os.makedirs(os.path.join(self.work, "TASK-26", config.CLONES[0], "ophis"))
        self.check()
        self.assertFalse(any(a[:len(LOCAL)] == LOCAL for a in self.argvs()))

    def test_slug_default_and_branch_validation(self):
        self.git_dir()
        self.assertEqual(self.check(title="Fix: it's 骨架").branch, "TASK-26-fix-it-s")
        for over, want in (({"local": ok("TASK-26-a\nTASK-26-b\n")}, target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b")),
                           ({"local": ok("TASK-26-Bad$(x)\n")}, target.Invalid("existing branch name 'TASK-26-Bad$(x)' is not TASK-26-<lowercase slug>")),
                           ({"local": ok("TASK-26-" + "a" * 41 + "\n")}, target.Invalid(f"existing branch name {'TASK-26-' + 'a' * 41!r} is not TASK-26-<lowercase slug>")),
                           ({"local": ok("TASK-26-" + "a" * 40 + "\n")}, target.Target("ophis", "agent-pm", "TASK-26-" + "a" * 40)),
                           ({"local": ok("TASK-27-x\n")}, target.Invalid("existing branch name 'TASK-27-x' is not TASK-26-<lowercase slug>"))):
            with self.subTest(want=want):
                self.assertEqual(self.check(**over), want)

    def test_several_branch_names_are_cut_at_200(self):
        self.git_dir()
        names = [f"TASK-26-{c}" + "a" * 40 for c in "abcdef"]
        r = self.check(local=ok("\n".join(names) + "\n"))
        self.assertEqual(r, target.Invalid("several TASK-26-* branches: " + ", ".join(names)[:200]))

    def test_branch_lookup_failures_are_transient(self):
        self.git_dir()
        want = target.Transient("git branch --list: fatal")
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(self.check(local=ok(code=128, stderr="fatal"), **extra), want)
        shutil.rmtree(self.clone)
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(self.check(remote=ok(code=128, stderr="no remote"), **extra), target.Transient("git ls-remote: no remote"))

    def test_mapping_without_repo_line(self):
        r = self.check(**self.MAPPED)
        self.assertEqual(r, target.Target("ophis", "agent-pm", "TASK-26-session-registry"))
        self.assertIn((("gh", "api", "repos/ophis/agent-pm"), repo.SHORT), self.run_.calls)

    def test_repo_line_wins_over_mapping(self):
        r = self.check(repos={PROJ: "ophis/other"}, project=PROJ)
        self.assertEqual((r.owner, r.name), ("ophis", "agent-pm"))
        self.assertNotIn(("gh", "api", "repos/ophis/other"), self.argvs())

    def test_bad_repo_line_is_not_replaced_by_mapping(self):
        self.assertEqual(self.check("Repo: nope", repos=self.REPOS, project=PROJ), target.Invalid("unreadable Repo line: 'nope'"))
        self.assertEqual(self.check("Repo: ophis/a\nRepo: ophis/b", repos=self.REPOS, project=PROJ), target.Invalid("several different Repo: values"))
        self.assertEqual(self.run_.calls, [])

    def test_no_mapping_for_the_project_is_invalid(self):
        for project in ("other", None):
            with self.subTest(project=project):
                self.assertEqual(self.check("no repo here", repos=self.REPOS, project=project), NO_LINE)
        self.assertEqual(self.check("no repo here", project=PROJ), NO_LINE)
        self.assertEqual(self.run_.calls, [])

    def test_mapping_that_is_not_owner_name_is_invalid(self):
        for value, shown in (("nope", "'nope'"), (42, "'42'")):
            with self.subTest(value=value):
                self.assertEqual(self.check("no repo here", repos={PROJ: value}, project=PROJ),
                                 target.Invalid(f"{target.MAPPED}{shown}: not <owner>/<name>"))
        self.assertEqual(self.run_.calls, [])

    def test_access_and_response_failures(self):
        default = lambda name: api(default=name, push=True)
        unsafe = target.Invalid("ophis/agent-pm: unsafe default branch name")
        for label, res, want, tagged in (
                ("HTTP 404", ok(code=1, stderr="gh: Not Found (HTTP 404)"), target.Invalid("ophis/agent-pm: not found or no access (HTTP 404)"), True),
                ("HTTP 403", ok(code=1, stderr="gh: Forbidden (HTTP 403)"), target.Invalid("ophis/agent-pm: not found or no access (HTTP 403)"), True),
                ("HTTP 502", ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"), target.Transient("gh api repos/ophis/agent-pm: gh: Bad Gateway (HTTP 502)"), False),
                ("no HTTP code", ok(code=1, stderr="error connecting to api.github.com"),
                 target.Transient("gh api repos/ophis/agent-pm: error connecting to api.github.com"), False),
                ("no push", api(push=False), target.Invalid("ophis/agent-pm: no push permission"), True),
                ("push null", ok(json.dumps({"full_name": "ophis/agent-pm", "default_branch": "main", "permissions": {"push": None}})),
                 target.Invalid("ophis/agent-pm: no push permission"), True),
                ("default -x", default("-x"), unsafe, True),
                ("default a..b", default("a..b"), unsafe, True),
                ("default ma$(x)", default("ma$(x)"), unsafe, True),
                ("default null", default(None), unsafe, True)):
            for issue_label, extra, prefix in self.ISSUES:
                with self.subTest(label, issue=issue_label):
                    self.assertEqual(self.check(api=res, **extra), target.Invalid(prefix + want.reason) if tagged else want)
                    self.assertEqual(self.argvs(), [("gh", "api", "repos/ophis/agent-pm")])

    def test_unusable_response_is_transient(self):
        good = {"full_name": "ophis/agent-pm", "default_branch": "main", "permissions": {"push": True}}
        without = lambda key: json.dumps({k: v for k, v in good.items() if k != key})
        for label, out in (("not JSON", "<html>"), ("empty", ""), ("a list", "[]"), ("null", "null"),
                           ("no permissions", without("permissions")), ("no default_branch", without("default_branch")),
                           ("no full_name", without("full_name")), ("permissions null", json.dumps({**good, "permissions": None})),
                           ("full_name not owner/name", json.dumps({**good, "full_name": "nope"})),
                           ("full_name not a string", json.dumps({**good, "full_name": 7}))):
            with self.subTest(label):
                r = self.check(api=ok(out))
                self.assertIsInstance(r, target.Transient)
                self.assertTrue(r.reason.startswith("gh api repos/ophis/agent-pm: "), r)
                self.assertEqual(self.argvs(), [("gh", "api", "repos/ophis/agent-pm")])

    def test_o_and_n_come_from_full_name(self):
        renamed = os.path.join(self.work, "TASK-26", config.CLONES[0], "Ophis", "Agent-PM-2")
        os.makedirs(os.path.join(renamed, ".git"))
        for desc, asked in (("Repo: OPHIS/agent-pm", "OPHIS/agent-pm"), ("Repo: https://github.com/ophis/Agent-PM.git", "ophis/Agent-PM")):
            with self.subTest(desc):
                r = self.check(desc, api=api("Ophis/Agent-PM-2"), local=ok("TASK-26-x\n"))
                self.assertEqual(r, target.Target("Ophis", "Agent-PM-2", "TASK-26-x"))
                self.assertEqual(self.run_.calls[0][0], ("gh", "api", f"repos/{asked}"))
                self.assertEqual(self.argvs()[1], self.branch_list(renamed))
        r = self.check(api=api("Ophis/Agent-PM-2"), remote=ok("abc\trefs/heads/TASK-26-y\n"))
        self.assertEqual(r, target.Target("Ophis", "Agent-PM-2", "TASK-26-y"))
        self.assertEqual(self.argvs()[-1][-2:], ("https://github.com/Ophis/Agent-PM-2.git", "TASK-26-*"))

    def test_mapping_failures_keep_the_prefix(self):
        want = target.Invalid(target.MAPPED + "ophis/agent-pm: not found or no access (HTTP 404)")
        self.assertEqual(self.check(api=ok(code=1, stderr="gh: Not Found (HTTP 404)"), **self.MAPPED), want)

def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)

class WithClone(unittest.TestCase):
    """Real git clones (no network) and hand-made checkouts in a realpath temp dir; `writable=()` unless a test is about it."""
    T = target.Target("ophis", "agent-pm", "TASK-26-x")
    HTTPS = "https://github.com/ophis/agent-pm.git"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.work = os.path.join(self.tmp, "work")
        self.checkout = os.path.join(self.work, "TASK-26", config.CLONES[0], "ophis", "agent-pm")

    def clone(self, name, origin=HTTPS):
        path = os.path.join(self.tmp, name)
        git("init", "-q", path)
        git("-C", path, "remote", "add", "origin", origin)
        return path

    def fresh(self):
        shutil.rmtree(self.checkout, ignore_errors=True)
        os.makedirs(self.checkout)
        return os.path.join(self.checkout, ".git")

    def git_file(self, text):
        with open(self.fresh(), "w") as f:
            f.write(text)

    def worktree(self, clone):
        """A checkout whose `.git` file names a gitdir whose `commondir` names `clone`'s `.git`."""
        admin = os.path.join(self.tmp, "admin")
        os.makedirs(admin, exist_ok=True)
        with open(os.path.join(admin, "commondir"), "w") as f:
            f.write(os.path.join(clone, ".git") + "\n")
        self.git_file(f"gitdir: {admin}\n")
        return admin

    def run_(self, clones, **kw):
        kw.setdefault("writable", ())
        return target.with_clone(self.T, "TASK-26", clones, work=self.work, **kw)

    def want(self, clone):
        return target.Target("ophis", "agent-pm", "TASK-26-x", clone)

    def test_without_a_checkout_the_table_entry_is_found_case_insensitively(self):
        y = self.clone("y")
        for key in ("ophis/agent-pm", "Ophis/Agent-PM"):
            with self.subTest(key):
                self.assertEqual(self.run_({"ophis/other": "/o", key: y}), self.want(y))

    def test_without_a_checkout_and_without_an_entry_there_is_no_clone(self):
        for clones in ({}, {"ophis/other": "/o", "ophis/agent-pm-2": "/p"}):
            with self.subTest(clones):
                self.assertEqual(self.run_(clones), self.want(""))
        os.makedirs(os.path.join(self.work, "TASK-26", config.CLONES[0], "ophis"))
        self.assertEqual(self.run_({}), self.want(""))

    def test_a_clone_made_from_owner_name_beats_the_table(self):
        os.makedirs(self.fresh())
        self.assertEqual(self.run_({"ophis/agent-pm": self.clone("y")}), self.want(""))

    def test_a_worktree_of_the_targets_clone_beats_the_table(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        for clones in ({"ophis/agent-pm": y}, {}):
            with self.subTest(clones):
                self.assertEqual(self.run_(clones), self.want(x))

    def test_the_clones_origin_is_matched_case_insensitively(self):
        x = self.clone("x", "git@github.com:Ophis/Agent-PM.git")
        self.worktree(x)
        self.assertEqual(self.run_({}), self.want(x))

    def test_the_only_git_run_is_the_guarded_origin_read_of_the_clone(self):
        x = self.clone("x")
        self.worktree(x)
        fake = FakeRun([(LOCAL, ok(self.HTTPS + "\n"))])
        self.assertEqual(self.run_({}, run=fake), self.want(x))
        self.assertEqual(fake.calls, [((*LOCAL, x, "remote", "get-url", "origin"), repo.SHORT)])
        for setup in (lambda: os.makedirs(self.fresh()), lambda: shutil.rmtree(self.checkout)):
            setup()
            fake.calls.clear()
            self.run_({"ophis/agent-pm": x}, run=fake)
            self.assertEqual(fake.calls, [])

    def test_a_clone_under_writable_is_not_used(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        self.assertEqual(self.run_({"ophis/agent-pm": y}, writable=(self.tmp,)), self.want(y))
        self.assertEqual(self.run_({"ophis/agent-pm": y}, writable=(os.path.join(self.tmp, "elsewhere"),)), self.want(x))
        self.assertEqual(self.run_({}, writable=(self.tmp,)), self.want(""))

    def test_the_default_writable_holds_the_work_dir(self):
        inside = os.path.join(self.work, "clone")
        git("init", "-q", inside)
        git("-C", inside, "remote", "add", "origin", self.HTTPS)
        y = self.clone("y")
        self.worktree(inside)
        self.assertEqual(self.run_({"ophis/agent-pm": y}, writable=None), self.want(y))

    def test_the_table_decides_when_the_worktrees_clone_is_not_usable(self):
        y = self.clone("y")
        other = self.clone("other", "https://github.com/ophis/other.git")
        gone = self.clone("gone")
        self.worktree(other)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.worktree(gone)
        shutil.rmtree(gone)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        newline = self.clone("x\ny")
        self.worktree(newline)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.git_file("nonsense\n")
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.git_file("gitdir: " + os.path.join(self.tmp, "no-such-gitdir") + "\n")
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))

    def test_a_symlinked_git_is_not_followed(self):
        x, y = self.clone("x"), self.clone("y")
        real_file = os.path.join(self.tmp, "dotgit-file")
        self.worktree(x)
        shutil.copy(os.path.join(self.checkout, ".git"), real_file)
        for dest in (real_file, os.path.join(x, ".git")):
            with self.subTest(dest):
                os.symlink(dest, self.fresh())
                self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
                self.assertEqual(self.run_({}), self.want(""))

    def test_owner_name_and_branch_are_unchanged(self):
        y = self.clone("y")
        r = self.run_({"ophis/agent-pm": y})
        self.assertEqual((r.owner, r.name, r.branch, self.T.clone), ("ophis", "agent-pm", "TASK-26-x", ""))

if __name__ == "__main__":
    unittest.main()
