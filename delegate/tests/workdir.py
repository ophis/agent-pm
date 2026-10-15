"""Workdir paths other projects' tests need; the layout stays delegate's."""

import os

from agent_pm.delegate.engine.types import Layout
from agent_pm.utils import checkout


def checkout_path(workdir: str, owner: str, name: str, slug: str) -> str:
    return checkout(os.path.join(Layout(workdir).agent, "src"), owner, name, slug)
