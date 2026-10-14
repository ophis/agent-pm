"""Codex launch codec and the hook commands shared by execution and trust inspection."""

import json
import os
import shlex
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from agent_pm.session.adapters.codex.common import launch_options, passed
from agent_pm.session.adapters.codex.hook_schema import HOOKS
from agent_pm.session.adapters.codex.types import HookPlan, LaunchPlan, Snapshot
from agent_pm.session.adapters.flags import split_flags
from agent_pm.utils import printable_abs

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


def _words(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(word, str) or "\0" in word for word in value
    ):
        raise ValueError("Codex tui launch: invalid command")
    return tuple(value)


def _roots(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not printable_abs(path) or not os.path.isdir(path) for path in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("Codex tui hook command requires distinct absolute source directories")
    return tuple(value)


def _paths(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(path, str) or "\0" in path or not os.path.isabs(path) for path in value
    ):
        raise ValueError(f"Codex tui launch: invalid {label} paths")
    return tuple(value)


def hook_plan(argv: Sequence[str]) -> HookPlan:
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError("Codex tui requires an absolute Tui mode hook command")
    try:
        roots = json.loads(argv[-1])
    except (ValueError, RecursionError) as exc:
        raise ValueError("Codex tui hook command requires source roots JSON") from exc
    return HookPlan(_words(list(argv[:-1])), _roots(roots))


def decode(data: object) -> LaunchPlan:
    if not isinstance(data, dict) or frozenset(data) != FIELDS:
        raise ValueError("Codex tui launch: invalid launch fields")
    selection, hook = _words(data["selection"]), _words(data["hook"])
    if not hook:
        raise ValueError("Codex tui launch: invalid command")
    executable = data["executable"]
    if (
        not isinstance(executable, str)
        or "\0" in executable
        or not os.path.isabs(executable)
        or not os.path.isabs(hook[0])
    ):
        raise ValueError("Codex tui launch: expected absolute executables")
    prompt = data["prompt"]
    if not isinstance(prompt, str) or "\0" in prompt:
        raise ValueError("Codex tui launch: invalid prompt")
    if any(
        not isinstance(data[key], str) or "\0" in data[key] or not os.path.isabs(data[key])
        for key in ("workdir", "store")
    ) or not isinstance(data["sid"], str):
        raise ValueError("Codex tui launch: invalid recovery identity")
    return LaunchPlan(
        executable,
        selection,
        prompt,
        HookPlan(hook, _roots(data["import_roots"])),
        data["workdir"],
        data["sid"],
        data["store"],
        _paths(data["protect"], "protect"),
    )


def encode(plan: LaunchPlan) -> str:
    return json.dumps(
        {
            "executable": plan.executable,
            "selection": list(plan.selection),
            "prompt": plan.prompt,
            "hook": list(plan.hook.prefix),
            "workdir": plan.workdir,
            "sid": plan.sid,
            "store": plan.store,
            "protect": list(plan.protect),
            "import_roots": list(plan.hook.import_roots),
        },
        ensure_ascii=False,
    )


def hook_argv(plan: HookPlan) -> tuple[str, ...]:
    return *plan.prefix, json.dumps(plan.import_roots)


def commands(plan: HookPlan) -> Mapping[str, str]:
    argv = hook_argv(plan)
    return MappingProxyType({event: shlex.join((*argv, event)) for event, _, _ in HOOKS})


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
