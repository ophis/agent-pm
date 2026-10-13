import json
import os
import re
import shlex
import shutil
import sys

from agent_pm.session.adapters.codex import sessions
from agent_pm.session.adapters.codex.common.types import (
    LIMIT_PREFIXES,
    CodexCommon,
    added_dirs,
    flags,
    native_id,
    passed,
    refuse,
    reset_at,
)
from agent_pm.session.adapters.codex.hook_schema import HOOKS
from agent_pm.session.adapters.codex.sessions import split_flags
from agent_pm.session.adapters.types import Tui
from agent_pm.session.engine.types import ConfigError, Launch, LimitHit, SessionRequest
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

    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        resume = request.resumed
        common = self.common
        self.state = common.prepare(request, persist=False)
        snapshot = self.state.snapshot
        saved_flags = tuple(snapshot["flags"])
        refuse(saved_flags)
        self.native = self.state.native_id or ""
        env = common.environment(snapshot, interactive=True)
        unset = common.UNSET
        effective_env = {key: value for key, value in os.environ.items() if key not in unset}
        effective_env.update(env)
        store = self.store = sessions.store_root()
        exe = shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
        if exe is None:
            raise ConfigError.codex_executable()
        exe = os.path.abspath(exe)
        if not hook or not os.path.isabs(hook[0]):
            raise ConfigError.codex_hook()
        roots = import_roots()
        dependencies = [
            *roots,
            sys.prefix,
            sys.base_prefix,
            sys.executable,
            exe,
            store,
            *(word for word in hook if os.path.isabs(word)),
            *spec.protect,
        ]
        added = added_dirs(saved_flags, snapshot["cwd"])
        try:
            sessions.protect(self.state, dependencies, effective_env, added)
        except sessions.RecoveryError as exc:
            raise ConfigError(str(exc)) from exc
        if not resume:
            self.state = common.save(self.state)
        commands = {event: shlex.join((*hook, event)) for event in self.hooks}
        hook_flags = [
            word
            for event, command in commands.items()
            for word in (
                "-c",
                (
                    f"hooks."
                    f"{event}"
                    f'=[{{hooks=[{{type="command",command='
                    f"{json.dumps(command, ensure_ascii=False)}"
                    f",timeout="
                    f"{(3 if event == 'Interrupt' else 10)}"
                    f"}}]}}]"
                ),
            )
        ]
        _, literal = split_flags(saved_flags)
        argv = [
            exe,
            *self.common.selector(self.native, resume=resume),
            "--no-daemon",
            "-C",
            snapshot["cwd"],
            "-c",
            'tui.notification_method="bel"',
            *flags(snapshot, interactive=True),
            *hook_flags,
            *(
                [*literal, *([prompt] if prompt else [])]
                if literal
                else (["--", prompt] if prompt else [])
            ),
        ]
        data = {
            "argv": argv,
            "hook": list(hook),
            "commands": commands,
            "hook_flags": [*hook_flags, *passed(saved_flags)],
            "workdir": self.state.workdir,
            "sid": self.state.sid,
            "store": store,
            "protect": [*roots, *spec.protect],
            "add_dirs": added,
        }
        return Launch(
            (
                os.path.realpath(sys.executable),
                "-I",
                os.path.join(DIRECTORY, "tui_start.py"),
                json.dumps(data, ensure_ascii=False),
            ),
            env=env,
            unset=unset,
            cwd=snapshot["cwd"],
            project=snapshot["project"],
            transcript=common.transcript(self.state) if resume else "",
            resume=common.resume_command(self.state) if resume else "",
        )

    def hook_state(self) -> dict:
        return {
            "adapter_hook": os.path.join(DIRECTORY, "tui_hook.py"),
            "adapter_state": {"store": self.store},
        }

    def session_id(self, payload):
        native, cwd = payload.get("session_id"), payload.get("cwd")
        if (
            payload.get("agent_id") is not None
            or not native_id(native)
            or not isinstance(cwd, str)
            or not os.path.isabs(cwd)
            or "\0" in cwd
            or self.native
            and native != self.native
            or self.state
            and os.path.realpath(cwd) != self.state.snapshot["cwd"]
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
