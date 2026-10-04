"""Agent Skill: writes the composed role + task as <out>/<role>-<task>/SKILL.md instead of starting a run."""
import json
import os

from .base import Client, Launch

TAIL = "\n---\n\nInput: $ARGUMENTS\nOutput: {output}\nWorkdir: a new temp dir (`mktemp -d`), made once per invocation\n"
TO_FILE = "the path the input names, else `./{name}-<date +%Y%m%d-%H%M>.md` in the current directory"
TO_CALLER = "your final reply to the caller: the frontmatter, then the document; no file"


class SkillClient(Client):
    keys = frozenset({"output", "description", "roles"})
    runs = False

    def launch(self, prompt, run, *, sid, resume, access, out):
        name = f"{run['role']}-{run['task']}"
        description = self.value(run, "description") or f"{run['task_title']} as {run['role_title']}: {run['task_summary']}"
        head = f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
        dest = TO_CALLER if run["output"]["type"] == "caller" else TO_FILE.format(name=name)
        text = head + prompt.rstrip() + "\n" + TAIL.format(output=dest)
        return Launch([], files={os.path.join(os.path.abspath(out), name, "SKILL.md"): text})
