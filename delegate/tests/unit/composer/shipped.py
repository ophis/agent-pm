import glob
import os
import re

from agent_pm.delegate import composer
from agent_pm.delegate.engine.config import CONFIG

from delegate.tests.case import REPO, Case
from delegate.tests.unit.composer.prompts import (
    composed,
    parameters,
)

TASKS = os.path.join(composer.TEAM, "tasks")

SCHEMA_SHA256 = "748847763f62d05e4412f1627a029ad88e42f004162361561d7556ec3877c366"

RUNTIME = re.compile(r"claude|codex", re.I)

FIXTURE = """\
language = "Chinese"

[roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""

API = [
    "ConfigError",
    "TEAM",
    "DELIVERABLE",
    "RunConfig",
    "RunParams",
    "Team",
    "team",
    "team_dirs",
    "index",
    "lookup",
    "path_for",
    "check_all",
    "catalog",
    "check_team_dirs",
    "check_overlap",
    "team_paths",
    "resolve",
    "trusted_dirs",
    "access",
    "render",
]

DROPPED = [
    "SRC",
    "SCHEMA",
    "PROGRESS",
    "OUTCOME",
    "CONFIG",
    "Intent",
    "roles",
    "tasks_path",
    "reference_errors",
    "bind",
    "fill",
    "outline",
    "unfenced",
    "anchor",
]

GENERIC_NAME = re.compile(r"(?<![/\w])(agent-pm|autopilot):[\w-]+|\bthe [\w-]+ skill\b")

LANGUAGE_RULE = "headings and fixed labels included"

REPO_ARGS = {
    "worktree": "--dir <Workdir>/src --branch <branch> [--name <checkout>] [--base <Base:>] <repo>",
    "status": "--dir <Workdir>/src --branch <branch> [--name <checkout>] [--base <Base:>] <repo>",
}

CHECKOUT_RULE = (
    "- **Checkout**: `[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>` the input's "
    "`Checkout:`; no `Checkout:` → drop it."
)

PICK = (
    "**Your task**: pick it from your charter's Tasks section as its opening sentence says; a task the input names "
    "wins. Read that task's file (no other task's), `<tasks>/<task>.md`, and follow its steps in order; its links "
    "(`#…`) point to sections of this prompt."
)

COMMANDS_RULE = (
    "- **Commands**: run each command this prompt gives exactly, written as Parameters says, as its own "
    "command (no `cd`, pipe, redirect or `&&`)."
)


def catalog() -> dict[str, dict[str, str]]:
    """The built-in team's catalog with the shipped config alone."""
    return composer.catalog(config=CONFIG, local=os.devnull)


def roles() -> list[str]:
    return list(catalog())


def role_tasks() -> list[tuple[str, str]]:
    return [(r, t) for r in roles() for t in catalog()[r]]


def runs() -> list[tuple[str, str | None]]:
    """Each role given no task, then each role and task."""
    return [(r, None) for r in roles()] + role_tasks()


def generic_names(text):
    return [line for line in text.splitlines() if GENERIC_NAME.search(line)]


def expanded(prompt):
    """`prompt` with each # Parameters name written as its value, as the rule says a run writes a
    command.
    """
    for name, value in parameters(prompt).items():
        prompt = prompt.replace(name, value.strip("`"))
    return prompt


def words(prompt):
    """
    `prompt` with this checkout's path, the machine's and not the team's text, written as <root>.
    """
    return prompt.replace(REPO, "<root>")


def guide(prompt):
    return prompt.split("\n# Principles\n", 1)[0]


def parameter_names(role, task=None, **changes):
    return list(parameters(composed(role, task, **changes)[0]))


def team_texts():
    """{path relative to the team root: text} of each .md file of the real team root (its tools/ are
    code).
    """
    out = {}
    for path in glob.glob(os.path.join(composer.TEAM, "**", "*.md"), recursive=True):
        with open(path) as f:
            out[os.path.relpath(path, composer.TEAM)] = f.read()
    return out


class RealConfig(Case):
    def setUp(self):
        self.local(FIXTURE)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(text)
