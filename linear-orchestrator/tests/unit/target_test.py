from agent_pm.linear_orchestrator.engine import target
from orchestrator_test_helpers.unit.target.data import NO_LINE
from orchestrator_test_helpers.unit.target.parse_repo_fixtures import ParseRepo as ParseRepoBase


class ParseRepo1(ParseRepoBase):
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
