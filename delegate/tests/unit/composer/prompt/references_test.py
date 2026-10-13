"""References behavior."""

import os

from agent_pm.delegate.composer.base import TEAM
from agent_pm.delegate.composer.prompt import (
    reference_errors,
)

from delegate.tests.case import Case
from delegate.tests.unit.composer.prompt.case import METHOD_NAMES
from delegate.tests.unit.composer.prompts import (
    method,
    role_sources,
    rule,
    task_text,
)


class References(Case):
    def errors(self, text):
        """The errors in `text` as a method file beside the real engineer prompt and task files."""
        own, tasks, methods = role_sources("engineer")
        return [
            e
            for e in reference_errors(own, tasks, methods | {"fixture": text})
            if e.startswith("fixture:")
        ]

    def test_a_link_to_a_path_of_the_prompt_and_a_task_file_reference_pass(self):
        for text in (
            "[Engineer › Repo › Merge](#repo), then",
            "[Engineer › Repo](#repo) step 1",
            "[Principles](#principles)",
            "[Principles › Work › Worktree](#work)",
            "[Engineer › Finish › Failure](#finish)",
            "[Output › Return](#return)",
            "([Engineer › Rules](#rules)' rule)",
            "(`<tasks>/<task>.md` › Steps › Which build picks the build)",
            "`<tasks>/build.md` › Steps",
            "[docs](https://example.com/a)",
            "[A › B](https://example.com/a)",
            "`Engineer › Repo` and ``a ` › b``",
            "```\nEngineer › Repo\n[x](../y.md)\n```",
        ):
            self.assertEqual(self.errors(text), [], text)

    def test_a_wrong_reference_is_an_error(self):
        for text in (
            "[Engineer › Nope](#nope)",
            "Engineer › Repo",
            "[Engineer › Repo](#finish)",
            "[x](../roles/engineer.md#repo)",
            "[x]()",
            "[Principles › Worktree](#work)",
            "[Engineer › Repo step 1](#repo)",
            "[Engineer › Repo](#repo) › Merge",
            "`<tasks>/<task>.md` › Steps › Nope",
            "`<tasks>/<task>.md` › Steps › Which builds",
            "`<tasks>/<task>.md` › Steps › Which build › Nope",
            "`<tasks>/<task>.md` › Nope",
            "`<tasks>/nope.md` › Steps",
            "Which build › Steps",
            "```\n```\nEngineer › Repo",
        ):
            self.assertTrue(self.errors(text), text)

    def test_errors_name_the_file_and_the_offender(self):
        errors = reference_errors(
            "# R\n\n## S\n\nR › S\n", {"x": "# X\n\n[A](b.md)\n"}, {"m.md": "[A › B](#nope)\n"}
        )
        self.assertEqual(
            errors,
            [
                "prompt: bare reference: R › S",
                "tasks/x.md: relative link target: [A](b.md)",
                "m.md: no such anchor: [A › B](#nope)",
            ],
        )

    def test_a_task_file_path_must_exist_in_each_task_file_named(self):
        tasks = {"a": "# A\n\n## Steps\n\n1. **Go:** x\n", "b": "# B\n\n## Steps\n"}
        for text, want in (
            ("`<tasks>/<task>.md` › Steps", True),
            ("`<tasks>/<task>.md` › Steps › Go", False),
            ("`<tasks>/a.md` › Steps › Go", True),
            ("`<tasks>/b.md` › Steps › Go", False),
        ):
            self.assertEqual(reference_errors("# R\n", tasks, {"m": text}) == [], want, text)

    def test_a_method_file_path_must_exist_in_that_method_file(self):
        methods = {"methods/a.md": "# A\n\n## Rules\n\n- **Voting**: x\n"}
        for text, want in (
            ("`<team>/methods/a.md` › Rules › Voting", True),
            ("`<team>/methods/a.md` › Rules", True),
            ("`<team>/methods/a.md` › Rules › Nope", False),
            ("`<team>/methods/b.md` › Rules", False),
            ("`<team>/methods/<task>.md` › Rules", False),
        ):
            self.assertEqual(reference_errors("# R\n", {}, methods | {"m": text}) == [], want, text)

    def test_a_section_name_of_the_prompt_in_parentheses_is_a_bare_reference(self):
        for text in (
            "decide it (Repo).",
            "(Merge)",
            "see (Finish) first",
            "(Worktree)",
            "(Autopilot)",
        ):
            self.assertTrue(self.errors(text), text)
        for text in (
            "(local, mixed)",
            "([Engineer › Repo](#repo))",
            "(`Repo`)",
            "(the Repo)",
            "(Repo step 1)",
            "```\n(Repo)\n```",
        ):
            self.assertEqual(self.errors(text), [], text)

    def test_texts_checks_only_the_given_texts_against_owns_anchors(self):
        own = "# R\n\n## S\n\nR › S\n"
        errors = reference_errors(own, {}, {}, texts={"a": "[x](#s)\n", "b": "[y](#nope)\n"})
        self.assertEqual(errors, ["b: no such anchor: [y](#nope)"])

    def test_labels_relabel_a_tasks_errors(self):
        errors = reference_errors(
            "# R\n", {"x": "[A](b.md)\n", "y": "[B](c.md)\n"}, {}, labels={"x": "/t/x.md"}
        )
        self.assertEqual(
            errors,
            [
                "/t/x.md: relative link target: [A](b.md)",
                "tasks/y.md: relative link target: [B](c.md)",
            ],
        )


class Methods(Case):
    def test_voting_has_its_home_in_ultracode_and_pipeline_shares_its_start(self):
        deep, ultra = (method(m) for m in METHOD_NAMES)
        for m, text in zip(METHOD_NAMES, (deep, ultra), strict=False):
            for name in ("Voting", "Pipeline"):
                self.assertEqual(len(rule(text, name)), 1, f"{m}: {name}")
            self.assertTrue(
                rule(text, "Pipeline")[0].startswith(
                    "- **Pipeline**: ≤ 10 subagents running at once. Each result of a stage goes to the next stage as soon "
                    "as it arrives, never in batches;"
                ),
                m,
            )
        self.assertEqual(
            rule(deep, "Voting"), ["- **Voting**: `<team>/methods/ultracode.md` › Rules › Voting."]
        )
        self.assertTrue(
            rule(ultra, "Pipeline")[0].endswith(
                "only verification waits for all claims, to rank them."
            )
        )
        for phrase in (
            "fetch selection waits for all search results",
            "verification waits for all claims",
        ):
            self.assertIn(phrase, rule(deep, "Pipeline")[0])

    def test_each_states_voting_pipeline_restrictions_and_its_own_limits(self):
        voting = (
            "≥ 2 refutes → refuted",
            "else ≥ 2 valid votes → confirmed",
            "else (agent errors, missing votes) → unverified",
            "votes that came back",
            "unsure → votes refuted",
        )
        both = (
            "≤ 10 subagents running at once",
            "verification waits for all claims",
            "the calling task's restrictions for",
            "into every subagent prompt, voters included",
        )
        own = {
            "deep-research": (
                "5 complementary web search angles",
                "after every search agent has returned",
                "rank the whole set by relevance (high → low)",
                "dispatch fetches for the first ≤ 15",
                "top 25",
                "≤ 100",
                "start each web agent with fresh context (no inherited conversation), so it sees "
                "only its prompt",
                "only from the brief and web results",
                "Dispatch fetches only for URLs a search agent returned",
                "**Page text**:",
            ),
            "ultracode": voting
            + (
                "A fixed method for the ultracode round",
                "room in the cap for the key claims' votes",
                "while ≥ 3 cap slots remain",
                "undispatched subquestions and unverified claims go under Gaps",
            ),
        }
        self.assertEqual(set(own), set(METHOD_NAMES))
        for m in METHOD_NAMES:
            text = method(m)
            with self.subTest(m):
                self.assertTrue(text.startswith("# "))
                self.assertNotRegex(text, r"\b[Yy]ou\b")
            for phrase in both + own[m]:
                with self.subTest(m, phrase=phrase):
                    self.assertIn(phrase, text)
        self.assertNotIn("as each search agent's results arrive", method("deep-research"))
        for phrase in voting:  # the Voting rule's one home is ultracode
            with self.subTest("deep-research", not_in=phrase):
                self.assertNotIn(phrase, method("deep-research"))

    def test_private_detail_has_its_home_in_the_researcher_charter(self):
        home = "with no **private detail** in queries: internal names, paths, permalinks, private repo names, "
        with open(os.path.join(TEAM, "roles", "researcher.md")) as f:
            self.assertIn(home, f.read())
        link = "private detail ([Researcher › Type and target › Agents](#type-and-target))"
        self.assertIn(link, task_text("deep-research"))
        self.assertIn(
            "private detail per [Researcher › Type and target › Agents](#type-and-target)",
            method("deep-research"),
        )
        for text in (task_text("deep-research"), method("deep-research")):
            self.assertNotIn("internal names", text)

    def test_the_researcher_charter_and_methods_are_harness_neutral(self):
        with open(os.path.join(TEAM, "roles", "researcher.md")) as f:
            texts = {"roles/researcher.md": (f.read(), ())} | {
                m: (method(m), ("{{",)) for m in METHOD_NAMES
            }
        for name, (text, more) in texts.items():
            for word in ("Read, Grep", "Glob", "Workflow tool", "journal.jsonl", *more):
                with self.subTest(name, word=word):
                    self.assertNotIn(word, text)
