from agent_pm.linear_orchestrator.engine import target
from orchestrator_tests.case import Case
from orchestrator_tests.unit.target_data import NO_LINE, PROJ, issue


class ParseRepo(Case):
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


class Slug(Case):
    def test_rules(self):
        self.assertEqual(target.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(target.slug("Engineering 骨架"), "engineering")
        self.assertEqual(target.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(target.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(target.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))


class PrTitle(Case):
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


class ResearchRepo(Case):
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

