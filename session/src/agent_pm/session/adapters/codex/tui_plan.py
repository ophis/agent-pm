"""Codex launch codec and the hook commands shared by execution and trust inspection."""

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType

from agent_pm.session.adapters.codex.common import launch_options, passed
from agent_pm.session.adapters.codex.hook_schema import HOOKS
from agent_pm.session.adapters.codex.types import HookPlan, LaunchPlan, Snapshot
from agent_pm.session.adapters.flags import split_flags

FIELDS = frozenset(
    {
        "executable",
        "selection",
        "prompt",
        "hook",
        "workdir",
        "sid",
        "store",
        "protect",
        "import_roots",
    }
)


def _strs(value: object, *, absolute: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(word, str) or "\0" in word or absolute and not os.path.isabs(word)
        for word in value
    ):
        raise ValueError(
            "Codex tui launch: expected absolute paths"
            if absolute
            else "Codex tui launch: invalid command"
        )
    return tuple(value)


def decode(data: object) -> LaunchPlan:
    if not isinstance(data, dict) or frozenset(data) != FIELDS:
        raise ValueError("Codex tui launch: invalid launch fields")
    selection, hook = _strs(data["selection"]), _strs(data["hook"])
    if any(word.startswith("-") for word in selection):
        raise ValueError("Codex tui launch: invalid session selection")
    if not hook or not os.path.isabs(hook[0]):
        raise ValueError("Codex tui launch: invalid command")
    executable, workdir, store = _strs(
        [data["executable"], data["workdir"], data["store"]], absolute=True
    )
    prompt = data["prompt"]
    if not isinstance(prompt, str) or "\0" in prompt:
        raise ValueError("Codex tui launch: invalid prompt")
    if not isinstance(data["sid"], str):
        raise ValueError("Codex tui launch: invalid recovery identity")
    return LaunchPlan(
        executable,
        selection,
        prompt,
        HookPlan(hook, tuple(data["import_roots"])),
        workdir,
        data["sid"],
        store,
        _strs(data["protect"], absolute=True),
    )


def encode(plan: LaunchPlan) -> str:
    data = asdict(plan)
    data["hook"] = plan.hook.argv
    data["import_roots"] = plan.hook.import_roots
    return json.dumps(data, ensure_ascii=False)


def commands(plan: HookPlan) -> Mapping[str, str]:
    return MappingProxyType({event: shlex.join((*plan.argv, event)) for event, _, _ in HOOKS})


def hook_flags(plan: HookPlan) -> tuple[str, ...]:
    return tuple(
        word
        for event, command in commands(plan).items()
        for word in (
            "-c",
            (
                f'hooks.{event}=[{{hooks=[{{type="command",command={json.dumps(command, ensure_ascii=False)},'
                f"timeout={3 if event == 'Interrupt' else 10}}}]}}]"
            ),
        )
    )


def trust_args(plan: LaunchPlan, snapshot: Snapshot) -> list[str]:
    return [*hook_flags(plan.hook), *passed(snapshot.flags)]


def execution_argv(plan: LaunchPlan, snapshot: Snapshot, trusted_state: str) -> list[str]:
    supplied, literal = split_flags(snapshot.flags)
    tail = (
        [*literal, *([plan.prompt] if plan.prompt else [])]
        if literal
        else (["--", plan.prompt] if plan.prompt else [])
    )
    return [
        plan.executable,
        *plan.selection,
        "--no-daemon",
        "-C",
        snapshot.cwd,
        "-c",
        'tui.notification_method="bel"',
        *supplied,
        *launch_options(snapshot),
        *hook_flags(plan.hook),
        "-c",
        trusted_state,
        *tail,
    ]
