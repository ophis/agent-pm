import json
import os
import re
import shlex
import shutil
import sys

from adapters.base import ACTIVITY, BLOCKED, TURN_END, ConfigError, Launch, LimitHit, Tui

from . import sessions
from .common import flags, native_id
from .headless import LIMIT_PREFIXES, reset_at

HOOKS = (
    ("SessionStart", "sessionStart"),
    ("UserPromptSubmit", "userPromptSubmit"),
    ("PostToolUse", "postToolUse"),
    ("PermissionRequest", "permissionRequest"),
    ("Stop", "stop"),
    ("Interrupt", "interrupt"),
)
DIRECTORY = os.path.dirname(os.path.abspath(__file__))


class CodexTuiRun(Tui):
    hooks = {
        "SessionStart": ACTIVITY,
        "UserPromptSubmit": ACTIVITY,
        "PostToolUse": ACTIVITY,
        "PermissionRequest": BLOCKED,
        "Stop": TURN_END,
        "Interrupt": TURN_END,
    }
    dialogs = (
        "Trust this folder?",
        "Config, hooks, and exec policies from untrusted folders stay disabled.",
        "Hooks need review",
        "Working directory · resume",
        "This conversation is open in another app",
        "Sign in with ChatGPT to use Codex",
    )

    def __init__(self, adapter):
        super().__init__(adapter)
        self.state = None
        self.last_reply = ""
        self.native = ""

    def launch(self, prompt, spec, resume, hook):
        common = self.adapter.common
        self.state = common.prepare(spec, resume, persist=False)
        snapshot = self.state.snapshot
        self.native = self.state.native_id or ""
        env = common.environment(snapshot, interactive=True)
        unset = common.interactive_unset()
        effective_env = {key: value for key, value in os.environ.items() if key not in unset}
        effective_env.update(env)
        store = sessions.store_root()
        exe = shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
        if exe is None:
            raise ConfigError("codex not found on PATH")
        exe = os.path.abspath(exe)
        if not hook or not os.path.isabs(hook[0]):
            raise ConfigError("Codex tui requires an absolute Driver hook command")
        dependencies = [
            os.path.dirname(os.path.dirname(DIRECTORY)),
            sys.prefix,
            sys.base_prefix,
            sys.executable,
            exe,
            store,
            *(word for word in hook if os.path.isabs(word)),
        ]
        try:
            sessions.protect(self.state, dependencies, effective_env)
        except sessions.RecoveryError as exc:
            raise ConfigError(str(exc)) from exc
        if not resume:
            self.state = common.save(self.state)
        commands = {
            event: shlex.join(
                (
                    os.path.realpath(sys.executable),
                    "-I",
                    os.path.join(DIRECTORY, "tui_hook.py"),
                    self.state.workdir,
                    self.state.sid,
                    store,
                    json.dumps(list(hook), ensure_ascii=False),
                    event,
                )
            )
            for event in self.hooks
        }
        hook_flags = [
            word
            for event, command in commands.items()
            for word in (
                "-c",
                f'hooks.{event}=[{{hooks=[{{type="command",command={json.dumps(command, ensure_ascii=False)},'
                f"timeout={3 if event == 'Interrupt' else 10}}}]}}]",
            )
        ]
        argv = [
            exe,
            *(["resume", self.native] if resume else []),
            "--no-daemon",
            "-C",
            snapshot["cwd"],
            "-c",
            'tui.notification_method="bel"',
            *flags(snapshot, interactive=True),
            *hook_flags,
            *(["--", prompt] if prompt else []),
        ]
        data = {
            "argv": argv,
            "hook": list(hook),
            "commands": commands,
            "hook_flags": hook_flags,
            "workdir": self.state.workdir,
            "sid": self.state.sid,
            "store": store,
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
            resume=common.resume_command(self.state) if resume else "",
        )

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
                        self.adapter.common.config.get("env", {}),
                    )
                )
        return None
