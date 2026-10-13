import os
import re

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import inputs, target
from agent_pm.linear_orchestrator.linear import issues
from orchestrator_tests.case import Case
from orchestrator_tests.unit.inputs_data import (
    ANN,
    BASE,
    BUILD_PRECEDENCE,
    DOCS,
    HUMANS,
    endpoint,
    fenced,
    issue,
    lines,
    note,
    ok,
)
from orchestrator_tests.unit.inputs_scenarios import Gh


class RenderBuild(Case):
    TARGET = target.Target("ophis", "agent-pm", "ENG-7-session-registry")

    PRD = inputs.Doc(
        BASE + "Product%20Design/2026-09-05-PM-9-q.md",
        "Product Design/2026-09-05-PM-9-q.md",
        "# PRD\n",
    )

    def notes(self):
        return (
            note("early user", "2026-09-02T00:00:00.000Z", ANN),
            note("agent early", "2026-09-03T00:00:00.000Z"),
            note("Build started: https://github.com/o/r/tree/b", "2026-09-04T00:00:00.000Z"),
            note("at the cutoff", "2026-09-04T00:00:00.000Z", ANN),
            note("Question: which?", "2026-09-05T00:00:00.000Z"),
            note("Use Redis.", "2026-09-06T00:00:00.000Z", ANN),
            note("bot late", "2026-09-07T00:00:00.000Z", (None, None)),
            note("Bob says more", "2026-09-08T00:00:00.000Z", ("bob@example.com", "Bob")),
        )

    def test_golden_direct_issue(self):
        iss = issue(
            lines(
                "Build the registry.", "Only the API.", "", "## Comments", "- Ann, t:", "  > quoted"
            ),
            ident="ENG-7",
            title="ENG: Session Registry!",
            notes=self.notes(),
        )
        src = inputs.Sources(
            (inputs.Doc(BASE + "Research/r.md", "Research/r.md", "r"), self.PRD), None
        )
        got = inputs.render(iss, "engineer", src, humans=HUMANS, target=self.TARGET, docs=DOCS)
        self.assertEqual(
            got,
            lines(
                "Reference: ENG-7",
                "Title: ENG-7: Session Registry",
                "Repo: ophis/agent-pm",
                "Checkout: ENG-7",
                "Branch: ENG-7-session-registry",
                f"Links: https://linear.app/t/issue/ENG-7, {self.PRD.url}",
                "",
                BUILD_PRECEDENCE,
                "",
                "## The user's requirements since the last build",
                "",
                (
                    "Ann, 2026-09-06T00:00:00.000Z:\nUse Redis.\n\nBob, "
                    "2026-09-08T00:00:00.000Z:\nBob says more"
                ),
                "",
                "## The user's instructions",
                "",
                "ENG: Session Registry!",
                "",
                "Build the registry.\nOnly the API.",
                "",
                "## PRD: `Product Design/2026-09-05-PM-9-q.md`",
                "",
                fenced("# PRD"),
                "",
                "## Linked documents (context)",
                "",
                "### `Research/r.md`",
                "",
                fenced("r"),
                "",
                "## Earlier comments (context; the user's ones are already built)",
                "",
                "- Ann, 2026-09-02T00:00:00.000Z:",
                "  > early user",
                "- Researcher, 2026-09-03T00:00:00.000Z:",
                "  > agent early",
                "- Researcher, 2026-09-04T00:00:00.000Z:",
                "  > Build started: https://github.com/o/r/tree/b",
                "- Ann, 2026-09-04T00:00:00.000Z:",
                "  > at the cutoff",
                "- Researcher, 2026-09-05T00:00:00.000Z:",
                "  > Question: which?",
                "- integration, 2026-09-07T00:00:00.000Z:",
                "  > bot late",
            ),
        )

    def test_golden_handoff_without_prd(self):
        head = lines(
            "Handoff from PM-9: u",
            "",
            "## Source",
            f"- PRD: {BASE}Product%20Design/p.md",
            "",
            "## Instructions",
            "Ann, 2026-09-01T00:00:00.000Z:",
            "Build the PRD.",
        )
        comments = lines("", "## Comments", "- Ann, 2026-09-01T00:00:00.000Z:", "  > hi")
        clone = target.Target("ophis", "agent-pm", "ENG-7-session-registry", "/x/agent-pm")
        for d, t, repo_line, tail in (
            (
                head + "\n" + comments,
                self.TARGET,
                "ophis/agent-pm",
                (
                    "",
                    "## Comments on PM-9 (context)",
                    "",
                    "- Ann, 2026-09-01T00:00:00.000Z:",
                    "  > hi",
                ),
            ),
            (head, clone, "/x/agent-pm", ()),
        ):
            with self.subTest(target=t):
                got = inputs.render(
                    issue(d, ident="ENG-7", title="ENG: Registry"),
                    "engineer",
                    inputs.Sources((), None),
                    humans=HUMANS,
                    target=t,
                    docs=DOCS,
                )
                self.assertEqual(
                    got,
                    lines(
                        "Reference: ENG-7",
                        "Title: ENG-7: Registry",
                        f"Repo: {repo_line}",
                        "Checkout: ENG-7",
                        "Branch: ENG-7-session-registry",
                        "Links: https://linear.app/t/issue/ENG-7",
                        "",
                        BUILD_PRECEDENCE,
                        "",
                        "## The user's instructions",
                        "",
                        "Ann, 2026-09-01T00:00:00.000Z:\nBuild the PRD.",
                        "",
                        "## PRD",
                        "",
                        "None linked.",
                        *tail,
                    ),
                )

    def test_handoff_prd_and_link_line(self):
        d = lines("Handoff from PM-9: u", "", "## Instructions", "Ann, t:", "Go.")
        src = inputs.Sources((self.PRD,), None)
        got = inputs.render(
            issue(d, ident="ENG-7"), "engineer", src, humans=HUMANS, target=self.TARGET, docs=DOCS
        )
        self.assertIn(f"Links: https://linear.app/t/issue/ENG-7, {self.PRD.url}\n", got)
        self.assertIn(
            lines(
                "## The user's instructions",
                "",
                "Ann, t:\nGo.",
                "",
                "## PRD: `Product Design/2026-09-05-PM-9-q.md`",
            ),
            got,
        )
        self.assertNotIn("## Linked documents", got)

    def test_only_a_design_dir_doc_is_the_prd_first_one_wins(self):
        other = inputs.Doc(
            BASE + "Product%20Design/2026-09-06-PM-10-z.md",
            "Product Design/2026-09-06-PM-10-z.md",
            "z",
        )
        report = inputs.Doc(BASE + "Research/r.md", "Research/r.md", "r")
        got = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            inputs.Sources((report, self.PRD, other), None),
            humans=HUMANS,
            target=self.TARGET,
            docs=DOCS,
        )
        self.assertIn("## PRD: `Product Design/2026-09-05-PM-9-q.md`", got)
        self.assertIn(
            "## Linked documents (context)\n\n### `Research/r.md`\n\n"
            + fenced("r")
            + "\n\n### `Product Design/2026-09-06-PM-10-z.md`",
            got,
        )
        self.assertNotIn("### `Product Design/2026-09-05-PM-9-q.md`", got)
        none = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            inputs.Sources((report,), None),
            humans=HUMANS,
            target=self.TARGET,
            docs=DOCS,
        )
        self.assertIn("## PRD\n\nNone linked.", none)
        self.assertIn("## Linked documents (context)\n\n### `Research/r.md`", none)

    def test_missing_prd_text_names_the_branch(self):
        prd = inputs.Doc(self.PRD.url, self.PRD.path, None)
        got = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            inputs.Sources((prd,), None),
            humans=HUMANS,
            target=self.TARGET,
            docs=DOCS,
        )
        self.assertIn("## PRD: `Product Design/2026-09-05-PM-9-q.md`\n\n(not found on main)", got)

    def test_without_build_started_the_cutoff_is_the_creation_time(self):
        ns = (
            note("one", "2026-09-02T00:00:00.000Z", ANN),
            note("old", "2026-08-30T00:00:00.000Z", ANN),
        )
        ns = tuple(sorted(ns, key=lambda n: n.at))
        got = inputs.render(
            issue("D", ident="ENG-7", notes=ns),
            "engineer",
            inputs.Sources((), None),
            humans=HUMANS,
            target=self.TARGET,
            docs=DOCS,
        )
        self.assertIn(
            (
                "## The user's requirements since the last build\n\nAnn, "
                "2026-09-02T00:00:00.000Z:\none"
            ),
            got,
        )
        self.assertIn(
            (
                "## Earlier comments (context; the user's ones are already built)\n\n- "
                "Ann, 2026-08-30T00:00:00.000Z:\n  > old"
            ),
            got,
        )


class HandoffWithACommentsLine(Case):
    def test_repo_links_and_instructions_agree(self):
        prd = RenderBuild.PRD.url
        d = "\n\n".join(
            [
                "Handoff from PM-9: https://linear.app/t/issue/PM-9",
                "## Instructions\nAnn, 2026-09-01T00:00:00.000Z:\nBuild it.\n## Comments\n"
                f"Repo: ophis/agent-pm\nPRD: {prd}",
                "## Comments\n- Ann, 2026-09-01T00:00:00.000Z:\n  > Repo: ophis/other\n"
                f"  > {BASE}Research/r.md",
            ]
        )
        iss = issue(d, ident="ENG-7", title="ENG: Registry")
        instructions = issues.parse_handoff(d).instructions
        self.assertTrue(instructions.endswith(f"\nRepo: ophis/agent-pm\nPRD: {prd}"))
        self.assertEqual(target.parse_repo(d), ("ophis", "agent-pm"))
        gh = Gh({endpoint("Product%20Design/2026-09-05-PM-9-q.md"): ok("# PRD\n")})
        src = inputs.gather(iss, "engineer", DOCS, run=gh)
        self.assertEqual(src, inputs.Sources((RenderBuild.PRD,), None))
        got = inputs.render(
            iss, "engineer", src, humans=HUMANS, target=RenderBuild.TARGET, docs=DOCS
        )
        self.assertIn(f"\nLinks: https://linear.app/t/issue/ENG-7, {prd}\n", got)
        self.assertIn(
            f"## The user's instructions\n\n{instructions}\n\n## PRD: `{RenderBuild.PRD.path}`", got
        )


class Checkout(Case):
    def test_a_build_prompts_repo_py_commands_take_the_inputs_checkout(self):
        t = target.Target("ophis", "agent-pm", "ENG-7-x")
        text = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            inputs.Sources((), None),
            humans=HUMANS,
            target=t,
            docs=DOCS,
        )
        name = re.search(r"^Checkout: (.+)$", text, re.M).group(1)
        self.assertEqual(
            os.path.basename(target.checkout("ENG-7", "ophis", "agent-pm", work="/w")),
            f"agent-pm-{name}",
        )
        for task in ("build", "light-build"):
            with self.subTest(task=task):
                run = delegate.resolve_run("engineer", task)
                spec = delegate.Spec(
                    text, self.home, task=task, out=os.path.join(self.home, "out.md")
                )
                prompt = delegate.plan(run, spec).prompt
                self.assertIn(f"\nCheckout: {name}\n", prompt)
                self.assertIn(
                    (
                        "`[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>`"
                        " the input's `Checkout:`"
                    ),
                    prompt,
                )
                for cmd in ("worktree", "status"):
                    self.assertIn(
                        (
                            f"python3 -P <team>/tools/repo.py {cmd}"
                            f" --dir <Workdir>/src --branch <branch> [--name <checkout>] [--base "
                            f"<Base:>] <repo>`"
                        ),
                        prompt,
                    )
