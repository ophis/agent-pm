"""act-as's Adapter, runtime `print_prompt`: in_process mode only (PrintPromptInProcess, in_process.py), which hands
the composed prompt back as the deliverable and starts no process. No config keys, no runtime note."""

from agent_pm.adapters.base import Adapter
from agent_pm.adapters.print_prompt.in_process import PrintPromptInProcess


class PrintPromptAdapter(Adapter):
    def headless(self):
        return None

    def tui(self):
        return None

    def in_process(self):
        return PrintPromptInProcess(self)
