"""Load Codex standalone dependencies without changing the import search path."""

import os
import runpy

DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOAD = runpy.run_path(os.path.join(DIRECTORY, "../../hosts/tmux/loading/types.py"))["load_file"]


def sibling(name):
    return LOAD("_agent_pm_codex_" + name, os.path.join(DIRECTORY, name + ".py"))
