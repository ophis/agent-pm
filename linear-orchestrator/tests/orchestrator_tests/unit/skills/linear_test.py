"""linear-orchestrator/skills/linear/SKILL.md: every issue command it shows is one
agent-pm-linear issue accepts, it shows every one, and its pre-approved rules only read.
"""

import contextlib
import io

from agent_pm.linear_orchestrator.engine import issue
from orchestrator_tests.case import Case
from orchestrator_tests.skills.linear import body, examples, front, rules

READS = ("read", "list", "lookup")


def _commands(parser):
    """Every leaf command of parser, as its words after `issue`."""
    found = []
    for action in parser._subparsers._group_actions:
        for name, child in action.choices.items():
            leaves = _commands(child) if child._subparsers else [[]]
            found += [[name, *leaf] for leaf in leaves]
    return found


class LinearSkill(Case):
    def test_every_example_parses_without_a_usage_error(self):
        self.assertGreater(len(examples()), 15)
        for argv in examples():
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                args = issue._parser().parse_args(argv)
                self.assertIsNone(issue._usage(args))

    def test_it_shows_every_issue_command(self):
        shown = {tuple(argv[: len(c)]) for argv in examples() for c in _commands(issue._parser())}
        for command in _commands(issue._parser()):
            self.assertIn(tuple(command), shown, command)

    def test_its_pre_approved_rules_only_read(self):
        self.assertTrue(rules())
        for rule in rules():
            with self.subTest(rule=rule):
                self.assertIn(rule.split()[2], READS)
                self.assertTrue(rule.startswith("agent-pm-linear issue "))

    def test_it_states_the_relation_direction_partial_create_and_archive_rules(self):
        text = body()
        for phrase in (
            "--blocks B` is `agent-pm-linear issue relation add B --blocked-by A",
            "recover",
            "never run `create` again",
            "--full --output",
            "--body-file -",
            "completed or canceled",
        ):
            self.assertIn(phrase, text)

    def test_the_commander_can_load_it(self):
        self.assertIn("name: linear\n", front())
        self.assertNotIn("disable-model-invocation", front())
