import os
import re
import shutil
import sys

from agent_pm.session.adapters.base import Tui
from agent_pm.session.adapters.codex import sessions, tui_plan
from agent_pm.session.adapters.codex.common import (
    LIMIT_PREFIXES,
    CodexCommon,
    added_dirs,
    refuse,
    reset_at,
)
from agent_pm.session.adapters.codex.hook_schema import HOOKS
from agent_pm.session.adapters.codex.types import LaunchPlan
from agent_pm.session.engine.types import ConfigError, Launch, LimitHit, Spec
from agent_pm.utils import import_roots

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


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
        try:
            hooks = tui_plan.hook_plan(hook, source_roots)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
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
            sessions.protect(self.state, dependencies, effective_env, added)
        except sessions.RecoveryError as exc:
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
                os.path.join(DIRECTORY, "tui_start.py"),
                tui_plan.encode(plan),
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
            "adapter_hook": os.path.join(DIRECTORY, "tui_hook.py"),
            "adapter_state": {"store": self.store},
        }

    def session_id(self, payload):
        native = sessions.hook_identity(payload)
        if (
            not native
            or self.native
            and native != self.native
            or self.state
            and not sessions.hook_cwd_matches(payload, self.state.snapshot.cwd)
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
