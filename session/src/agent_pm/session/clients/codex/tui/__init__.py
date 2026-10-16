"""Codex interactive launch plans, hook callbacks and event interpretation.
Each new assistant message in the bound native session file since launch is one Reply, read from a
cursor; a Stop's text that lands later is not repeated.
"""

import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType

import agent_pm.utils
from agent_pm.session.clients.base import Tui
from agent_pm.session.clients.codex.common import (
    LIMIT_PREFIXES,
    VALUED,
    CodexCommon,
    RecoveryError,
    SessionStore,
    launch_options,
    native_id,
    reset_at,
)
from agent_pm.session.clients.codex.common.types import Snapshot
from agent_pm.session.clients.common import scan, split_flags
from agent_pm.session.engine.types import (
    ClientBound,
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    Launch,
    LimitHit,
    Reply,
    SessionEvent,
    Spec,
)
from agent_pm.utils import json_records, module_roots, read_regular, source_path

PASSED = ("-c", "--enable", "--disable")

START = f"{__name__}.start"  # run as python -P -s -m on this layer's source roots

CLIENT_PYTHONPATH = "AGENT_PM_CLIENT_PYTHONPATH"  # START's env: the client's PYTHONPATH, never argv


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
    }
)


@dataclass(frozen=True)
class HookPlan:
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LaunchPlan:
    executable: str
    selection: tuple[str, ...]
    prompt: str
    hook: HookPlan
    workdir: str
    sid: str
    store: str


def _hook_identity(value) -> tuple[str, str] | None:
    identifier, path = value.get("session_id"), value.get("cwd")
    if (
        value.get("agent_id") is not None
        or not native_id(identifier)
        or not isinstance(path, str)
        or not os.path.isabs(path)
        or "\0" in path
    ):
        return None
    return identifier, os.path.realpath(path)


# Each dialog's title, and the first option or instruction Codex renders under it.
DIALOGS = MappingProxyType(
    {
        "Trust this folder?": "1. Trust and continue",
        "Config, hooks, and exec policies from untrusted folders stay disabled.": "1. Open restricted",
        "Hooks need review": "1. Review hooks",
        "Working directory · resume": "1. Use session directory",
        "This conversation is open in another app": "Close it there and press R to continue here.",
        "Sign in with ChatGPT to use Codex": "1. Sign in with ChatGPT",
    }
)


def _starts_line(text: str, screen: str) -> bool:
    """`text` starts a line of `screen`, after the line's marks (`›`, `🔒`) and its wrapping."""
    words = r"\s+".join(map(re.escape, text.split()))
    return re.search(rf"^[^\w\n]*{words}", screen, re.M) is not None


class CodexTui(Tui):
    hooks = MappingProxyType({label: HookKind(kind) for label, _, kind in HOOKS})
    dialogs = tuple(DIALOGS)

    def __init__(self, common: CodexCommon):
        self.common = common
        self.state = None
        self.last_reply = ""
        self.native = ""
        self.store = ""
        self.transcript, self.start, self.offset = "", 0, 0  # the native session file's cursor
        self.last = self.unseen = ""  # the turn's newest text; its Stop text not yet read

    def launch(self, spec: Spec, hook):
        common = self.common
        prompt, resume = spec.prompt, spec.resume
        self.state = common.prepare(spec)
        snapshot = self.state.snapshot
        self.native = self.state.native_id or ""
        env = common.environment(snapshot, interactive=True)
        store = self.store = common.store.root
        exe = shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
        if exe is None:
            raise ConfigError("codex not found on PATH")
        exe = os.path.abspath(exe)
        self.transcript = common.transcript(self.state) if resume else ""
        self.start = self.offset = os.path.getsize(self.transcript) if self.transcript else 0
        plan = LaunchPlan(
            exe,
            self.common.selector(self.native, resume=resume),
            prompt,
            HookPlan(tuple(hook)),
            self.state.workdir,
            self.state.sid,
            store,
        )
        pythonpath = env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
        try:
            encoded = encode(plan)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        launch = Launch(
            (os.path.realpath(sys.executable), "-P", "-s", "-m", START, encoded),
            env={
                **env,
                **source_path(module_roots(sys.modules[__name__], agent_pm.utils)),
                **({CLIENT_PYTHONPATH: pythonpath} if pythonpath is not None else {}),
            },
            unset=(*common.UNSET, CLIENT_PYTHONPATH),
            cwd=snapshot.cwd,
            project=snapshot.project,
            transcript=self.transcript,
            resume=common.resume_command(self.state) if resume else "",
        )
        return launch

    def commit(self) -> None:
        self.state = self.common.commit(self.state)

    def hook_callback(self) -> HookCallback:
        return HookCallback(__name__, {"store": self.store, "start": self.start})

    def restore(self, spec, state):
        """The launching CodexTui's store, recovery state and native id, from the store it named."""
        store, start = state.get("store"), state.get("start", 0)
        if type(start) is not int or start < 0:
            raise ConfigError("codex tui state: bad transcript cursor")
        self.start = self.offset = start
        try:
            sessions = SessionStore(store if isinstance(store, str) else None)
            self.store = sessions.root
            self.state = sessions.load(spec.workdir, spec.sid)
        except (RecoveryError, OSError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc
        self.native = "" if self.state is None else self.state.native_id or ""

    def observe(self):
        if not self.transcript and self.native and self.state is not None:
            try:
                self.transcript = self.common.transcript(replace(self.state, native_id=self.native))
            except (ConfigError, OSError):
                return []
        lines = (read_regular(self.transcript, offset=self.offset) or b"").split(b"\n")[:-1]
        self.offset += sum(len(line) + 1 for line in lines)  # a line still being written waits
        return [reply for record in json_records(lines) for reply in self._record(record)]

    def replay(self, records):
        """Ends in the latest turn's state: a Stop since the hook log's last prompt leaves its text
        unseen unless it is the session file's newest.
        """
        found: list[SessionEvent | ClientBound] = [*self.observe()]
        newest, ended = self.last, None
        for label, payload in records:
            found += self.hook_events(label, payload)
            if label in ("UserPromptSubmit", "Stop"):
                ended = self.last_reply if label == "Stop" else None
        self.last = newest if ended is None else ""
        self.unseen = ended if ended and ended.strip() and ended != newest else ""
        return found

    def _record(self, record: dict) -> list[Reply]:
        payload = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            return []
        if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
            return []
        if payload["role"] == "user":  # a prompt: a new turn
            self.last = self.unseen = ""
            return []
        content = payload.get("content")
        text = "\n".join(
            block["text"]
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict)
            and block.get("type") == "output_text"
            and isinstance(block.get("text"), str)
        )
        if text.strip() and text.strip() == self.unseen.strip():
            self.unseen = ""
        elif text.strip():
            self.last = text
            return [Reply(text)]
        return []

    def session_id(self, payload):
        if self.native and payload.get("session_id") != self.native:
            return ""
        identity = _hook_identity(payload)
        if identity is None:
            return ""
        native, cwd = identity
        if self.state and cwd != self.state.snapshot.cwd:
            return ""
        self.native = native
        if payload.get("hook_event_name") == "Stop":
            message = payload.get("last_assistant_message")
            self.last_reply = message if isinstance(message, str) else ""
        return native

    def hook_events(self, label, payload):
        if label not in self.hooks or not isinstance(payload, dict):
            return []
        if payload.get("hook_event_name", label) != label:
            return []
        payload = {**payload, "hook_event_name": label}
        if not self.session_id(payload):
            return []
        return super().hook_events(label, payload)

    def reply(self, payload):
        if payload.get("hook_event_name") != "Interrupt":
            message = payload.get("last_assistant_message")
            self.last_reply = message if isinstance(message, str) else ""
            self.unseen = self.last_reply if self.last_reply.strip() != self.last.strip() else ""
            self.last = ""
        return self.last_reply

    def activity(self, payload):
        value = payload.get("tool_name")
        return value if isinstance(value, str) else ""

    def blocked_reason(self, payload):
        return self.activity(payload)

    def shows(self, dialog, screen):
        """Its title and first option each start a line: the title quoted in output is no dialog."""
        return _starts_line(dialog, screen) and _starts_line(DIALOGS[dialog], screen)

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
        HookPlan(hook),
        workdir,
        data["sid"],
        store,
    )


def encode(plan: LaunchPlan) -> str:
    data = asdict(plan)
    data["selection"] = list(plan.selection)
    data["hook"] = list(plan.hook.argv)
    decode(data)
    encoded = json.dumps(data, ensure_ascii=False)
    encoded.encode("utf-8")
    return encoded


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


def passed(flags) -> list[str]:
    return [
        word
        for flag in scan(flags, VALUED)
        if flag.word in PASSED and flag.value is not None
        for word in (flag.word, flag.value)
    ]


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
    if label not in CodexTui.hooks or value.get("hook_event_name", label) != label:
        return False
    try:
        identity = _hook_identity(value)
        if identity is None:
            return False
        native, cwd = identity
        store = SessionStore(root)
        state = store.load(workdir, sid)
        if state is None:
            raise RecoveryError("missing Codex recovery state")
        if cwd != state.snapshot.cwd:
            return False
        store.bind(workdir, sid, native, timeout=1 if label == "Interrupt" else 5)
    except (RecoveryError, OSError, ValueError) as exc:
        print(f"codex hook: {exc}", file=sys.stderr)
        return False
    return True


def on_hook(call: HookCall) -> dict | None:
    if not bind(call.label, call.payload, call.workdir, call.sid, root=call.state["store"]):
        return None
    return {**call.payload, "hook_event_name": call.label}
