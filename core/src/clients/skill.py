"""Agent Skill: writes the composed role + task as <dest>/<role>-<task>/SKILL.md, plus a copy of each core script it
runs under scripts/, instead of starting a run."""
import json
import os
import re

from compose import RunConfig

from .base import REPORT, Client, Launch

TAIL = "\n---\n\nInput: $ARGUMENTS\nWorkdir: the dir `mktemp -d` prints, run once at the start and reused for this invocation\n"
SCRIPTS = "${CLAUDE_SKILL_DIR}/scripts"
CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A skill runs once, inline: a task's Resume section (for interrupted runs) never applies.
RESUME = re.compile(r"\n## Resume\n.*?(?=\n# |\Z)", re.S)


class SkillClient(Client):
    keys = frozenset({"description", "roles"})
    runs = False

    def scripts_path(self, root: str) -> str:
        return SCRIPTS

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
        return Launch([], files=files)
