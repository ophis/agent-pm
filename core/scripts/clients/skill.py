"""Agent Skill: writes the composed role + task as <out>/<role>-<task>/SKILL.md, plus a copy of each core script it runs
under scripts/, instead of starting a run."""
import json
import os
import re

from .base import Client, Launch

TAIL = "\n---\n\nInput: $ARGUMENTS\nOutput: {output}\nWorkdir: the dir `mktemp -d` prints, run once at the start and reused for this invocation\n"
TO_FILE = "the path after `Output:` in the input, else `./{name}-$(date +%Y%m%d-%H%M).md` in the current directory"
SCRIPTS = "${CLAUDE_SKILL_DIR}/scripts"
CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TO_ORCHESTRATOR = "your final reply in this conversation: the frontmatter, then the deliverable; no file"
# A skill runs once, inline: a task's Resume section (for interrupted runs) never applies.
RESUME = re.compile(r"\n## Resume\n.*?(?=\n# |\Z)", re.S)


class SkillClient(Client):
    keys = frozenset({"description", "roles"})
    runs = False

    def scripts(self, root, run, out):
        return SCRIPTS

    def launch(self, prompt, run, *, sid, resume, access, out):
        name = f"{run['role']}-{run['task']}"
        description = self.value(run, "description") or f"{run['task_title']} as {run['role_title']}: {run['task_summary']}"
        head = f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
        dest = TO_ORCHESTRATOR if run["output"]["type"] == "orchestrator" else TO_FILE.format(name=name)
        text = head + RESUME.sub("\n", prompt).rstrip() + "\n" + TAIL.format(output=dest)
        skill = os.path.join(os.path.abspath(out), name)
        files = {os.path.join(skill, "SKILL.md"): text}
        for script in sorted(set(re.findall(re.escape(SCRIPTS) + r"/([\w.-]+)", text))):
            with open(os.path.join(CORE_SCRIPTS, script)) as f:
                files[os.path.join(skill, "scripts", script)] = f.read()
        return Launch([], files=files)
