"""Codex interactive launch plans, hook callbacks and event interpretation."""

import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType

from agent_pm.session.adapters.base import Tui
from agent_pm.session.adapters.codex.common import (
    LIMIT_PREFIXES,
    CodexCommon,
    RecoveryError,
    SessionStore,
    added_dirs,
    hook_cwd_matches,
    hook_identity,
    launch_options,
    passed,
    protect,
    refuse,
    reset_at,
)
from agent_pm.session.adapters.codex.common.types import HookPlan, LaunchPlan, Snapshot
from agent_pm.session.adapters.common import split_flags
from agent_pm.session.engine.types import ConfigError, Launch, LimitHit, Spec
from agent_pm.utils import import_roots

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


HOOKS = (
    ("SessionStart", "sessionStart", "activity"),
    ("UserPromptSubmit", "userPromptSubmit", "activity"),
    ("PostToolUse", "postToolUse", "activity"),
    ("PermissionRequest", "permissionRequest", "blocked"),
    ("Stop", "stop", "turn_end"),
    ("Interrupt", "interrupt", "turn_end"),
)


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


class CodexTui(Tui):
    hooks = {label: kind for label, _, kind in HOOKS}
    dialogs = (
        "Trust this folder?",
        "Config, hooks, and exec policies from untrusted folders stay disabled.",
        "Hooks need review",
        "Working directory · resume",
        "This conversation is open in another app",
        "Sign in with ChatGPT to use Codex",
    )

    def __init__(self, common: CodexCommon):
        self.common = common
        self.state = None
        self.last_reply = ""
        self.native = ""
        self.store = ""

    def launch(
        self, prompt, spec: Spec, hook, *, source_roots: tuple[str, ...], resume: bool = False
    ):
        common = self.common
        self.state = common.prepare(spec, persist=False, resume=resume)
        snapshot = self.state.snapshot
        saved_flags = snapshot.flags
        refuse(saved_flags)
        self.native = self.state.native_id or ""
        env = common.environment(snapshot, interactive=True)
        unset = common.UNSET
        effective_env = common.environment(snapshot, interactive=True, ambient=True)
        store = self.store = common.store.root
        exe = shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
        if exe is None:
            raise ConfigError("codex not found on PATH")
        exe = os.path.abspath(exe)
        hooks = HookPlan(tuple(hook), source_roots)
        roots = hooks.import_roots
        protected_roots = tuple(dict.fromkeys([*import_roots(), *roots]))
        dependencies = [
            *protected_roots,
            sys.prefix,
            sys.base_prefix,
            sys.executable,
            exe,
            store,
            *(word for word in hook if os.path.isabs(word)),
            *spec.protect,
        ]
        added = added_dirs(saved_flags, snapshot.cwd)
        try:
            protect(self.state, dependencies, effective_env, added)
        except RecoveryError as exc:
            raise ConfigError(str(exc)) from exc
        if not resume:
            self.state = common.save(self.state)
        plan = LaunchPlan(
            exe,
            self.common.selector(self.native, resume=resume),
            prompt,
            hooks,
            self.state.workdir,
            self.state.sid,
            store,
            (*protected_roots, *spec.protect),
        )
        return Launch(
            (
                os.path.realpath(sys.executable),
                "-I",
                os.path.join(DIRECTORY, "start.py"),
                encode(plan),
            ),
            env=env,
            unset=unset,
            cwd=snapshot.cwd,
            project=snapshot.project,
            transcript=common.transcript(self.state) if resume else "",
            resume=common.resume_command(self.state) if resume else "",
        )

    def hook_state(self) -> dict:
        return {
            "adapter_hook": __name__,
            "adapter_state": {"store": self.store},
        }

    def session_id(self, payload):
        native = hook_identity(payload)
        if (
            not native
            or self.native
            and native != self.native
            or self.state
            and not hook_cwd_matches(payload, self.state.snapshot.cwd)
        ):
            return ""
        self.native = native
        if payload.get("hook_event_name") == "Stop":
            message = payload.get("last_assistant_message")
            self.last_reply = message if isinstance(message, str) else ""
        return native

    def hook_events(self, label, payload, bound=""):
        if label not in self.hooks or not isinstance(payload, dict):
            return []
        if payload.get("hook_event_name", label) != label:
            return []
        payload = {**payload, "hook_event_name": label}
        if not self.session_id(payload):
            return []
        return super().hook_events(label, payload, bound)

    def reply(self, payload):
        if payload.get("hook_event_name") != "Interrupt":
            message = payload.get("last_assistant_message")
            self.last_reply = message if isinstance(message, str) else ""
        return self.last_reply

    def activity(self, payload):
        value = payload.get("tool_name")
        return value if isinstance(value, str) else ""

    def blocked_reason(self, payload):
        return self.activity(payload)

    def limit(self, screen):
        screen = screen.replace("’", "'")
        pattern = (
            r"(?m)(?:^[ \t]*|(?<=[ \t]))■[ \t]*"
            r"(?=You've hit your usage|Quota exceeded|Your workspace is out|You hit your spend cap)"
        )
        for match in re.finditer(pattern, screen):
            message = " ".join(screen[match.end() :].split())
            if message.startswith(LIMIT_PREFIXES):
                reset = re.search(r"(?:Try|try) again at [^.]+\.", message)
                return LimitHit(
                    reset_at(
                        reset.group() if reset else message,
                        self.common.config.get("env", {}),
                    )
                )
        return None


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


def bind(label, value, workdir, sid, *, root=None):
    if (
        label not in CodexTui.hooks
        or value.get("hook_event_name", label) != label
        or not hook_identity(value)
    ):
        return False
    try:
        store = SessionStore(root)
        state = store.load(workdir, sid)
        if state is None:
            raise RecoveryError("missing Codex recovery state")
        if not hook_cwd_matches(value, state.snapshot.cwd):
            return False
        store.bind(workdir, sid, value["session_id"], timeout=1 if label == "Interrupt" else 5)
    except (RecoveryError, OSError, ValueError) as exc:
        print(f"codex hook: {exc}", file=sys.stderr)
        return False
    return True


def on_hook(label, payload, state):
    if not bind(
        label,
        payload,
        state["workdir"],
        state["sid"],
        root=state["adapter_state"]["store"],
    ):
        return False
    payload["hook_event_name"] = label
    return True
