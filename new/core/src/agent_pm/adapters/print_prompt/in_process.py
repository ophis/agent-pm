"""PrintPromptInProcess: refuses resume (no session to continue). Its run writes the prompt byte-exact (UTF-8) to a new
<workdir>/tmp/prompt-*.md, then replies START and a done outcome block naming that file as deliverable_file."""

import json
import os
import tempfile

from agent_pm.adapters.base import ConfigError, InProcess, Reply, TurnEnd
from agent_pm.utils.protocol import OUTCOME, PROGRESS


class PrintPromptInProcess(InProcess):
    START = f"[{PROGRESS}:start] the composed prompt is handed to the caller"

    def check(self, prompt, spec, resume):
        if resume:
            raise ConfigError("print_prompt has no session to resume")

    def run(self, prompt, spec, resume):
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
        block = f"```{OUTCOME}\n{json.dumps(data)}\n```"
        yield Reply(self.START)
        yield Reply(block)
        yield TurnEnd(block, 0)
