"""A run's workdir as tests outside delegate need it, its layout staying delegate's: where a run's
checkout of a repo lives (the team text's <Workdir>/src/<owner>/<name>-<slug>).
"""

import os

from agent_pm.delegate.engine.types import Layout
from agent_pm.utils import checkout


def checkout_path(workdir: str, owner: str, name: str, slug: str) -> str:
    return checkout(os.path.join(Layout(workdir).agent, "src"), owner, name, slug)
