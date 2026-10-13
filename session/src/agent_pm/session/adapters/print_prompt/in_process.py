"""PrintPromptInProcess: refuses resume (no session to continue) and a Spec without the caller's
reply protocol (Spec.reply_tags). Its run writes the prompt byte-exact (UTF-8) to a new
<workdir>/tmp/prompt-*.md, then replies a start mark and a done outcome block naming that file as
deliverable_file, in those tags.
"""

import json
import os
import tempfile

from agent_pm.session.adapters.base import InProcess
from agent_pm.session.engine.types import ConfigError, Reply, Spec, TurnEnd

START = "the composed prompt is handed to the caller"


class PrintPromptInProcess(InProcess):
    def check(self, prompt: str, spec: Spec, *, resume: bool = False):
        if resume:
            raise ConfigError("print_prompt has no session to resume")
        if spec.reply_tags is None:
            raise ConfigError("print_prompt needs the caller's reply protocol (Spec.reply_tags)")

    def run(self, prompt, spec: Spec, *, resume: bool = False):
        if spec.reply_tags is None:
            raise ConfigError("print_prompt needs the caller's reply protocol (Spec.reply_tags)")
        progress, outcome = spec.reply_tags
        tmp = os.path.join(spec.workdir, "tmp")
        os.makedirs(tmp, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="prompt-", suffix=".md", dir=tmp)
        with os.fdopen(fd, "wb") as f:
            f.write(prompt.encode())
        data = {
            "status": "done",
            "title": "Composed prompt",
            "summary": "The prompt is composed and handed to the caller, who runs the task.",
            "deliverable_file": f"tmp/{os.path.basename(path)}",
        }
        block = f"```{outcome}\n{json.dumps(data)}\n```"
        yield Reply(f"[{progress}:start] {START}")
        yield Reply(block)
        yield TurnEnd(block, 0)
