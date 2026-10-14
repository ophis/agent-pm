"""manager/skills/{tmux,manage}/SKILL.md as a commander reads them: sections, `/agent-pm:tmux`
references, pre-approved rules and the layer command examples in backticks.
"""

import os
import re

from manager.tests.case import REPO

SKILLS = os.path.join(REPO, "manager", "skills")
BIN = os.path.join(REPO, "bin")
COMMAND = re.compile(r"`(agent-pm-[a-z]+\b[^`]*)`")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
REFERENCE = re.compile(r"`/agent-pm:tmux` › ([A-Z][a-z]+)")
RULES = re.compile(r"^  - Bash\((.+)\)$", re.M)


def text(skill: str) -> str:
    with open(os.path.join(SKILLS, skill, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def body(skill: str) -> str:
    return text(skill).split("\n---\n", 1)[1]


def rules(skill: str) -> list[str]:
    return RULES.findall(text(skill).split("\n---\n", 1)[0])


def sections(skill: str) -> list[str]:
    return re.findall(r"^## (.+)$", text(skill), re.M)


def section(skill: str, name: str) -> str:
    return re.search(rf"^## {name}\n(.*?)(?=^## |\Z)", text(skill), re.M | re.S)[1]


def references(skill: str) -> list[str]:
    return REFERENCE.findall(text(skill))


def spans(source: str) -> list[list[list[str]]]:
    """Each backticked example's layer commands, each as its words up to a bare `--` that hands the
    rest to a client; a nested `-- agent-pm-…` starts the next command.
    """
    found = []
    for span in COMMAND.findall(source):
        words = span.split()
        starts = [i for i, w in enumerate(words) if w.startswith("agent-pm-")]
        commands = []
        for start, end in zip(starts, [*starts[1:], len(words)], strict=True):
            command = words[start:end]
            commands.append(command[: command.index("--")] if "--" in command else command)
        found.append(commands)
    return found


def examples(source: str) -> list[list[str]]:
    return [command for commands in spans(source) for command in commands]


def subcommand(command: list[str]) -> str | None:
    word = command[1] if len(command) > 1 else ""
    return word if re.fullmatch(r"[a-z][a-z-]*", word) else None


def flags(command: list[str]) -> list[str]:
    return FLAG.findall(" ".join(command[1:]))
