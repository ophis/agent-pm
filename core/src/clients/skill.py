"""Agent Skill: the composed role + task, printed for the calling Claude Code conversation to follow (the act-as skill)
instead of starting an agent run."""
import re

from .base import PROGRESS, Client

TAIL = ("\n---\n\nInput: given with this prompt\n"
        "Workdir: the dir `mktemp -d` prints, run once at the start and reused for this invocation\n")
# A skill runs once, inline: a task's Resume section (for interrupted agent runs) never applies.
RESUME = re.compile(r"\n## Resume\n.*?(?=\n# |\Z)", re.S)
REPORT = (f"At each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, send a text message "
          "containing only that line: the mark, then your report")


class SkillClient(Client):
    keys = frozenset({"roles"})
    runs = False

    def handover(self) -> str:
        return ("End with your final reply in this conversation: the outcome's fields as YAML frontmatter, with its "
                f"`deliverable` after the frontmatter instead of in it; write no file for the outcome. {REPORT}.")

    def inline(self, prompt: str) -> str:
        return RESUME.sub("\n", prompt).rstrip() + "\n" + TAIL
