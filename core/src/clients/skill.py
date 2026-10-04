"""Agent Skill: writes the composed role + task as <dest>/<role>-<task>/SKILL.md, plus a copy of each core script it
runs under scripts/ and, when it names methods/, of core's team/methods/ under methods/, instead of starting a run."""
import json
import os
import re
from pathlib import Path

from compose import TEXT, RunConfig

from .base import PROGRESS, Client, Launch

TAIL = "\n---\n\nInput: $ARGUMENTS\nWorkdir: the dir `mktemp -d` prints, run once at the start and reused for this invocation\n"
SCRIPTS = "${CLAUDE_SKILL_DIR}/scripts"
METHODS = "${CLAUDE_SKILL_DIR}/methods"
CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_METHODS = os.path.join(os.path.dirname(CORE_SCRIPTS), TEXT, "methods")
# A skill runs once, inline: a task's Resume section (for interrupted runs) never applies.
RESUME = re.compile(r"\n## Resume\n.*?(?=\n# |\Z)", re.S)
REPORT = (f"At each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, send a text message "
          "containing only that line: the mark, then your report")


class SkillClient(Client):
    keys = frozenset({"description", "roles"})
    runs = False

    def scripts_path(self, root: str) -> str:
        return SCRIPTS

    def methods_path(self, root: str) -> str:
        return METHODS

    def handover(self) -> str:
        return ("End with your final reply in this conversation: the outcome's fields as YAML frontmatter, with its "
                f"`deliverable` after the frontmatter instead of in it; write no file for the outcome. {REPORT}.")

    def export(self, prompt: str, run: RunConfig, *, dest: str) -> Launch:
        name = f"{run.role}-{run.task}"
        description = self.value(run, "description") or f"{run.task_title} as {run.role_title}: {run.task_summary}"
        head = f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
        text = head + RESUME.sub("\n", prompt).rstrip() + "\n" + TAIL
        skill = os.path.join(os.path.abspath(dest), name)
        files = {os.path.join(skill, "SKILL.md"): text}
        for script in sorted(set(re.findall(re.escape(SCRIPTS) + r"/([\w.-]+)", text))):
            with open(os.path.join(CORE_SCRIPTS, script)) as f:
                files[os.path.join(skill, "scripts", script)] = f.read()
        if METHODS in text:
            for path in sorted(p for p in Path(CORE_METHODS).rglob("*") if p.is_file()):
                files[os.path.join(skill, "methods", path.relative_to(CORE_METHODS))] = path.read_text()
        return Launch([], files=files)
