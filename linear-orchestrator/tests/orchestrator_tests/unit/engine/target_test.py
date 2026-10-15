"""Repository descriptions, target validation and branch selection."""

import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, issues, target
from agent_pm.linear_orchestrator.engine.types import Issue
from agent_pm.utils import SHORT
from orchestrator_tests.case import Case
from orchestrator_tests.config_fixtures import HARNESS_DELEGATE
from orchestrator_tests.target_fixtures import LOCAL
from orchestrator_tests.unit.target_fixtures import FakeRun, api, ok

PROJ = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

NO_LINE = target.Invalid("no Repo: line in the description or its ## Instructions")


def roles():
    """Each role's tasks: the committed core config's index (no local file)."""
    index = HARNESS_DELEGATE.roles()
    return {r: SimpleNamespace(tasks=tuple(index[r])) for r in config.TASKS}


def issue(description, title="ENG: Session Registry", ident="TASK-26", project=None):
    return Issue(
        id="uuid-26",
        identifier=ident,
        url=f"https://linear.app/t/issue/{ident}",
        title=title,
        description=description,
        created_at="2026-09-01T00:00:00.000Z",
        project_id=project,
        notes=(),
        links=(),
        linked=(),
        handoff=issues.parse_handoff(description),
    )


class RepositoryDescription(Case):
    def test_accepted(self):
        for line in (
            "Repo: ophis/agent-pm",
            "repo: https://github.com/ophis/agent-pm",
            "REPO: https://github.com/ophis/agent-pm.git/",
            "Repo: git@github.com:ophis/agent-pm.git",
            "repo: [https://github.com/ophis/agent-pm](<https://github.com/ophis/agent-pm>)",
            "Repo: [agent-pm](https://github.com/ophis/agent-pm)",
            "  repo:   ophis/agent-pm  ",
            "`Repo: ophis/agent-pm`",
            "* Repo: ophis/agent-pm",
            "- Repo: ophis/agent-pm",
            "> Repo: ophis/agent-pm",
            "**Repo:** ophis/agent-pm",
            "**Repo: ophis/agent-pm**",
            "Repo: `ophis/agent-pm`",
        ):
            with self.subTest(line=line):
                self.assertEqual(target.parse_repo(f"x\n{line}\ny"), ("ophis", "agent-pm"))
        for d, want in (
            ("a\r\nRepo: ophis/agent-pm\r\nb", ("ophis", "agent-pm")),
            ("Repo: ophis/a\nrepo: OPHIS/A", ("ophis", "a")),
            ("Repo: ophis/.github", ("ophis", ".github")),
            ("Repo: https://github.com/ophis/.github.git", ("ophis", ".github")),
            ("Repo: ophis/..x", ("ophis", "..x")),
            ("Repo: ophis/a.git", ("ophis", "a.git")),
            ("Repo: git@github.com:ophis/a", ("ophis", "a")),
            ("Repo: https://github.com/ophis/a/", ("ophis", "a")),
        ):
            with self.subTest(d=d):
                self.assertEqual(target.parse_repo(d), want)

    def test_comments_section_ignored(self):
        d = "Repo: ophis/a\n\n## Comments\n* x:\n  > Repo: ophis/b"
        self.assertEqual(target.parse_repo(d), ("ophis", "a"))
        self.assertIsInstance(target.parse_repo("## Comments\nRepo: ophis/a"), target.Invalid)

    def test_rejected(self):
        for d in (
            *(
                f"Repo: {v}"
                for v in (
                    "",
                    "ophis",
                    "-ophis/a",
                    "ophis/-a",
                    "ophis/..",
                    "ophis/.",
                    "a b/c",
                    "ophis/a;rm",
                    "https://gitlab.com/ophis/a",
                    "http://github.com/ophis/a",
                    "ssh://git@github.com/ophis/a",
                    "git@github.com:ophis/a/",
                    "github.com/ophis/a",
                    "ophis/a/",
                    "https://github.com/ophis/a/tree/main",
                    "https://github.com/ophis/a?x=1",
                )
            ),
            "Repo: ophis/a\nRepo: ophis/b",
        ):
            with self.subTest(d=d):
                self.assertIsInstance(target.parse_repo(d), target.Invalid)

    def test_no_line_and_messages(self):
        self.assertEqual(target.NO_LINE, NO_LINE)
        self.assertEqual(target.parse_repo(None), NO_LINE)
        self.assertEqual(
            target.parse_repo("Repo: nope"), target.Invalid("unreadable Repo line: 'nope'")
        )
        self.assertEqual(
            target.parse_repo("Repo: ophis/a\nRepo: ophis/b"),
            target.Invalid("several different Repo: values"),
        )


class RepositorySlug(Case):
    def test_rules(self):
        self.assertEqual(target.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(target.slug("Engineering 骨架"), "engineering")
        self.assertEqual(target.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(target.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(target.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))


class PullRequestTitles(Case):
    def test_keeps_unicode_letters_and_digits(self):
        self.assertEqual(
            target.pr_title("TASK-38", "ENG: Engineering 骨架"), "TASK-38: Engineering 骨架"
        )
        self.assertEqual(target.pr_title("TASK-38", "ENG: 中文标题 2"), "TASK-38: 中文标题 2")

    def test_removes_what_single_quotes_cannot_hold(self):
        self.assertEqual(
            target.pr_title(
                "TASK-38", "ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮"
            ),
            "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h",
        )


class ResearchRepository(Case):
    REPOS = {PROJ: "ophis/agent-pm"}

    def test_mapped_repo_only_without_a_repo_line(self):
        for desc, project, repos, want in (
            ("no repo here", PROJ, self.REPOS, target.Target("ophis", "agent-pm")),
            ("", PROJ, self.REPOS, target.Target("ophis", "agent-pm")),
            ("Repo: ophis/other", PROJ, self.REPOS, None),
            ("Repo: nope", PROJ, self.REPOS, None),
            ("Repo: ophis/a\nRepo: ophis/b", PROJ, self.REPOS, None),
            ("no repo here", "other", self.REPOS, None),
            ("no repo here", None, self.REPOS, None),
            ("no repo here", PROJ, {}, None),
        ):
            with self.subTest(desc=desc, project=project, repos=repos):
                self.assertEqual(target.research_repo(issue(desc, project=project), repos), want)


class TargetValidation(Case):
    REPOS = {PROJ: "ophis/agent-pm"}

    MAPPED = {"desc": "no repo here", "repos": REPOS, "project": PROJ}

    ISSUES = (
        ("Repo line", {}, ""),
        ("Repo line, mapped project", {"repos": REPOS, "project": PROJ}, ""),
        ("mapped project", MAPPED, "project mapping "),
    )

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.src = os.path.join(self.work, "TASK-26", "agent", config.CLONES[0])
        self.clone = os.path.join(self.src, "ophis", "agent-pm-TASK-26")
        self.ls_remote = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "ls-remote",
            "--heads",
            "https://github.com/ophis/agent-pm.git",
            "TASK-26-*",
        ]

    def git_dir(self):
        os.makedirs(os.path.join(self.clone, ".git"))

    def git_file(self):
        os.makedirs(self.clone)
        with open(os.path.join(self.clone, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/agent-pm\n")

    def branch_list(self, at=None):
        at = at or self.clone
        return (
            *LOCAL,
            at,
            f"--git-dir={at}/.git",
            "branch",
            "--list",
            "TASK-26-*",
            "--format=%(refname:short)",
        )

    def check(
        self,
        desc="Repo: ophis/agent-pm",
        repos=None,
        project=None,
        title="ENG: Session Registry",
        **over,
    ):
        t = {"api": api(), "local": ok(""), "remote": ok("")}
        t.update(over)
        self.run_ = FakeRun(
            [
                (("gh", "api"), t["api"]),
                (LOCAL, t["local"]),
                (("git", "-c", "credential.helper="), t["remote"]),
            ]
        )
        return target.check(
            issue(desc, title=title, project=project),
            repos or {},
            run=self.run_,
            work=self.work,
            roles=roles(),
        )

    def argvs(self):
        return [c[0] for c in self.run_.calls]

    def test_the_checkout_is_in_the_agent_dir(self):
        self.assertEqual(
            target.checkout("TASK-26", "ophis", "agent-pm", work=self.work),
            os.path.join(self.work, "TASK-26", "agent", "src", "ophis", "agent-pm-TASK-26"),
        )

    def test_ok_without_a_local_clone_asks_the_remote(self):
        self.assertEqual(
            self.check(), target.Target("ophis", "agent-pm", "TASK-26-session-registry")
        )
        self.assertEqual(
            self.run_.calls,
            [
                (("gh", "api", "repos/ophis/agent-pm"), SHORT),
                (tuple(self.ls_remote), SHORT),
            ],
        )

    def test_default_runner_uses_the_current_path_and_environment(self):
        seen = []

        def subprocess_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return api() if argv[0] == "gh" else ok("")

        with (
            mock.patch.dict(os.environ, {"AGENT_PM_PATH": "/target-tools", "TARGET_ENV": "kept"}),
            mock.patch("subprocess.run", side_effect=subprocess_run),
        ):
            expected_path = config.path()
            self.assertEqual(
                target.check(issue("Repo: ophis/agent-pm"), {}, work=self.work, roles=roles()),
                target.Target("ophis", "agent-pm", "TASK-26-session-registry"),
            )
        self.assertEqual(len(seen), 2)
        for _, kwargs in seen:
            self.assertEqual(kwargs["env"]["PATH"], expected_path)
            self.assertEqual(kwargs["env"]["TARGET_ENV"], "kept")
            self.assertEqual(kwargs["timeout"], SHORT)

    def test_where_the_branches_are_read(self):
        elsewhere = os.path.join(self.work, "elsewhere")
        os.makedirs(os.path.join(elsewhere, ".git"))

        def link(dest):
            os.makedirs(self.clone)
            os.symlink(dest, os.path.join(self.clone, ".git"))

        def at(*parts):
            return lambda: os.makedirs(os.path.join(self.src, *parts))

        local, remote = [(self.branch_list(), SHORT)], [(tuple(self.ls_remote), SHORT)]
        cases = [
            ("clone with branches", self.git_dir, "TASK-26-local", local),
            ("clone without branches", self.git_dir, "", local + remote),
            ("worktree", self.git_file, "TASK-26-planted", remote),
            (
                "symlinked .git",
                lambda: link(os.path.join(elsewhere, ".git")),
                "TASK-26-local",
                local,
            ),
            (
                "dangling .git symlink",
                lambda: link(os.path.join(elsewhere, "gone")),
                "TASK-26-planted",
                remote,
            ),
            ("legacy agent-pm", at("agent-pm", ".git"), "TASK-26-old", remote),
            ("legacy ophis/agent-pm", at("ophis", "agent-pm", ".git"), "TASK-26-old", remote),
            ("no clone dir", at("ophis"), "TASK-26-old", remote),
        ]
        for label, setup, out, calls in cases:
            with self.subTest(label):
                shutil.rmtree(os.path.join(self.work, "TASK-26"), ignore_errors=True)
                setup()
                r = self.check(local=ok(out + "\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
                self.assertEqual(
                    r.branch, "TASK-26-local" if out == "TASK-26-local" else "TASK-26-remote"
                )
                self.assertEqual(self.run_.calls[1:], calls)

    def test_remote_names_are_taken_after_refs_heads(self):
        r = self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/tags/x\n"))
        self.assertEqual(r.branch, "TASK-26-a")
        self.assertEqual(
            self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/heads/TASK-26-b\n")),
            target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"),
        )

    def test_read_only_task_branches_are_not_the_build_branch(self):
        read_only = "TASK-26-deep-research\nTASK-26-light-research\nTASK-26-product-design\n"
        self.git_dir()
        for out, branch, remote in (
            ("TASK-26-session-registry\n" + read_only, "TASK-26-session-registry", False),
            (read_only, "TASK-26-session-registry", True),
            (
                "TASK-26-light-research-2\nTASK-26-light-research\n",
                "TASK-26-light-research-2",
                False,
            ),
            ("TASK-26-build\nTASK-26-light-research\n", "TASK-26-build", False),
        ):
            with self.subTest(out=out):
                self.assertEqual(
                    self.check(local=ok(out)), target.Target("ophis", "agent-pm", branch)
                )
                self.assertEqual(
                    self.argvs()[1:], [self.branch_list()] + [tuple(self.ls_remote)] * remote
                )

    def test_slug_default_and_branch_validation(self):
        self.git_dir()
        self.assertEqual(self.check(title="Fix: it's 骨架").branch, "TASK-26-fix-it-s")
        names = [f"TASK-26-{c}" + "a" * 40 for c in "abcdef"]
        for over, want in (
            (
                {"local": ok("TASK-26-a\nTASK-26-b\n")},
                target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"),
            ),
            (
                {"local": ok("TASK-26-Bad$(x)\n")},
                target.Invalid(
                    "existing branch name 'TASK-26-Bad$(x)' is not TASK-26-<lowercase slug>"
                ),
            ),
            (
                {"local": ok("TASK-26-" + "a" * 41 + "\n")},
                target.Invalid(
                    f"existing branch name {'TASK-26-' + 'a' * 41!r} is not TASK-26-<lowercase slug>"
                ),
            ),
            (
                {"local": ok("TASK-26-" + "a" * 40 + "\n")},
                target.Target("ophis", "agent-pm", "TASK-26-" + "a" * 40),
            ),
            (
                {"local": ok("TASK-27-x\n")},
                target.Invalid("existing branch name 'TASK-27-x' is not TASK-26-<lowercase slug>"),
            ),
            (
                {"local": ok("".join(n + "\n" for n in names))},
                target.Invalid("several TASK-26-* branches: " + ", ".join(names)[:200]),
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.check(**over), want)

    def test_branch_lookup_failures_are_transient(self):
        self.git_dir()
        want = target.Transient("git branch --list: fatal")
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(self.check(local=ok(code=128, stderr="fatal"), **extra), want)
        shutil.rmtree(self.clone)
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(
                    self.check(remote=ok(code=128, stderr="no remote"), **extra),
                    target.Transient("git ls-remote: no remote"),
                )

    def test_repo_line_or_project_mapping(self):
        found = target.Target("ophis", "agent-pm", "TASK-26-session-registry")
        gh = ("gh", "api", "repos/ophis/agent-pm")
        for desc, repos, project, want in (
            ("no repo here", self.REPOS, PROJ, found),
            ("Repo: ophis/agent-pm", {PROJ: "ophis/other"}, PROJ, found),
            ("Repo: nope", self.REPOS, PROJ, target.Invalid("unreadable Repo line: 'nope'")),
            (
                "Repo: ophis/a\nRepo: ophis/b",
                self.REPOS,
                PROJ,
                target.Invalid("several different Repo: values"),
            ),
            ("no repo here", self.REPOS, "other", NO_LINE),
            ("no repo here", self.REPOS, None, NO_LINE),
            ("no repo here", {}, PROJ, NO_LINE),
            (
                "no repo here",
                {PROJ: "nope"},
                PROJ,
                target.Invalid("project mapping 'nope': not <owner>/<name>", mapped=True),
            ),
            (
                "no repo here",
                {PROJ: 42},
                PROJ,
                target.Invalid("project mapping '42': not <owner>/<name>", mapped=True),
            ),
        ):
            with self.subTest(desc=desc, repos=repos, project=project):
                self.assertEqual(self.check(desc, repos=repos, project=project), want)
                self.assertEqual(self.argvs()[:1], [gh] if want == found else [])
                self.assertNotIn(("gh", "api", "repos/ophis/other"), self.argvs())

    def test_access_and_response_failures(self):
        def default(name):
            return api(default=name, push=True)

        unsafe = target.Invalid("ophis/agent-pm: unsafe default branch name")
        for label, res, want, tagged in (
            (
                "HTTP 404",
                ok(code=1, stderr="gh: Not Found (HTTP 404)"),
                target.Invalid("ophis/agent-pm: not found or no access (HTTP 404)"),
                True,
            ),
            (
                "HTTP 403",
                ok(code=1, stderr="gh: Forbidden (HTTP 403)"),
                target.Invalid("ophis/agent-pm: not found or no access (HTTP 403)"),
                True,
            ),
            (
                "HTTP 502",
                ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"),
                target.Transient("gh api repos/ophis/agent-pm: gh: Bad Gateway (HTTP 502)"),
                False,
            ),
            (
                "no HTTP code",
                ok(code=1, stderr="error connecting to api.github.com"),
                target.Transient("gh api repos/ophis/agent-pm: error connecting to api.github.com"),
                False,
            ),
            (
                "no push",
                api(push=False),
                target.Invalid("ophis/agent-pm: no push permission"),
                True,
            ),
            (
                "push null",
                ok(
                    json.dumps(
                        {
                            "full_name": "ophis/agent-pm",
                            "default_branch": "main",
                            "permissions": {"push": None},
                        }
                    )
                ),
                target.Invalid("ophis/agent-pm: no push permission"),
                True,
            ),
            ("default -x", default("-x"), unsafe, True),
            ("default a..b", default("a..b"), unsafe, True),
            ("default ma$(x)", default("ma$(x)"), unsafe, True),
            ("default null", default(None), unsafe, True),
        ):
            for issue_label, extra, prefix in self.ISSUES:
                with self.subTest(label, issue=issue_label):
                    self.assertEqual(
                        self.check(api=res, **extra),
                        target.Invalid(prefix + want.reason, mapped=bool(prefix))
                        if tagged
                        else want,
                    )
                    self.assertEqual(self.argvs(), [("gh", "api", "repos/ophis/agent-pm")])

    def test_unusable_response_is_transient(self):
        good = {
            "full_name": "ophis/agent-pm",
            "default_branch": "main",
            "permissions": {"push": True},
        }

        def without(key):
            return json.dumps({k: v for k, v in good.items() if k != key})

        for label, out in (
            ("not JSON", "<html>"),
            ("empty", ""),
            ("a list", "[]"),
            ("null", "null"),
            ("no permissions", without("permissions")),
            ("no default_branch", without("default_branch")),
            ("no full_name", without("full_name")),
            ("permissions null", json.dumps({**good, "permissions": None})),
            ("full_name not owner/name", json.dumps({**good, "full_name": "nope"})),
            ("full_name not a string", json.dumps({**good, "full_name": 7})),
        ):
            with self.subTest(label):
                r = self.check(api=ok(out))
                self.assertIsInstance(r, target.Transient)
                self.assertTrue(r.reason.startswith("gh api repos/ophis/agent-pm: "), r)
                self.assertEqual(self.argvs(), [("gh", "api", "repos/ophis/agent-pm")])

    def test_o_and_n_come_from_full_name(self):
        renamed = os.path.join(self.src, "Ophis", "Agent-PM-2-TASK-26")
        os.makedirs(os.path.join(renamed, ".git"))
        for desc, asked in (
            ("Repo: OPHIS/agent-pm", "OPHIS/agent-pm"),
            ("Repo: https://github.com/ophis/Agent-PM.git", "ophis/Agent-PM"),
        ):
            with self.subTest(desc):
                r = self.check(desc, api=api("Ophis/Agent-PM-2"), local=ok("TASK-26-x\n"))
                self.assertEqual(r, target.Target("Ophis", "Agent-PM-2", "TASK-26-x"))
                self.assertEqual(self.run_.calls[0][0], ("gh", "api", f"repos/{asked}"))
                self.assertEqual(self.argvs()[1], self.branch_list(renamed))
        r = self.check(api=api("Ophis/Agent-PM-2"), remote=ok("abc\trefs/heads/TASK-26-y\n"))
        self.assertEqual(r, target.Target("Ophis", "Agent-PM-2", "TASK-26-y"))
        self.assertEqual(
            self.argvs()[-1][-2:], ("https://github.com/Ophis/Agent-PM-2.git", "TASK-26-*")
        )
