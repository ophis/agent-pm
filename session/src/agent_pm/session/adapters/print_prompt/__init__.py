"""act-as's Adapter, runtime `print_prompt`: in_process mode only (PrintPromptInProcess,
in_process.py), which hands the composed prompt back as the deliverable and starts no process. No
config keys, no runtime note.
"""

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.print_prompt.in_process import PrintPromptInProcess


class PrintPromptAdapter(Adapter):
    def in_process(self):
        return PrintPromptInProcess()
