"""Workdir paths other projects' tests need; the layout stays delegate's."""

import os

from agent_pm.delegate.engine.types import Layout
from agent_pm.utils import checkout


def session_cwd(workdir: str) -> str:
    return Layout(workdir).agent


def checkout_root(workdir: str) -> str:
    return os.path.join(Layout(workdir).agent, "src")


def checkout_path(workdir: str, owner: str, name: str, slug: str) -> str:
    return checkout(checkout_root(workdir), owner, name, slug)


def run_log(workdir: str) -> str:
    return os.path.join(Layout(workdir).run, "run.jsonl")
