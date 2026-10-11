"""The Adapter interface. An Adapter is one runtime's abstract factory, built by src/driver/registry.py from the core
config's [adapters.<name>] table minus `implementation`; headless_run() and tui_run() give the runtime's HeadlessRun or
TuiRun, or None when it lacks that Mode. A run turns a prompt and a Spec into a Launch, and the runtime's output into
the frozen event set (Event): SessionBound, Activity, Reply, TurnEnd, Blocked, LimitHit, Exited. Stderr, a diagnostic,
sits beside it. The Adapter also gives its account usage probe's command (usage_probe) and reads that probe's stdout as
Usage, and a session's SessionStatus. A spec type refuses a bad value with ConfigError when built; sid_error is the one
sid rule. Nothing here starts a process or touches tmux; imports no other layer."""
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from utils.paths import printable_abs

EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    """A config or spec error, raised by adapters and the Driver."""


def _strings(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


def _clean(value) -> bool:
    return isinstance(value, str) and "\0" not in value


def sid_error(sid) -> str | None:
    """Why `sid` is no agent-pm session id (a non-empty printable str without "/"); None when it is one."""
    if isinstance(sid, str) and sid and sid.isprintable() and "/" not in sid:
        return None
    return f'sid must be a non-empty printable string without "/", got {sid!r:.80}'


@dataclass(frozen=True)
class Access:
    """An agent run's runtime-neutral access intent; what each field grants is the adapter's to check."""
    dirs: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    cwd: str = ""           # the dir the run starts in; "": the workdir
    project: bool = False   # whether the runtime loads cwd's project settings and instructions

    def __post_init__(self):
        for name in ("dirs", "commands"):
            if not _strings(getattr(self, name)):
                raise ConfigError(f"{name} must be a tuple of strings")


@dataclass(frozen=True, kw_only=True)
class Spec:
    """What a session runs with."""
    workdir: str   # absolute; holds run.jsonl
    access: Access = Access()
    tier: int | None = None
    effort: str | None = None
    sid: str | None = None   # agent-pm's session id, set by the Driver's start(), never by the caller

    def __post_init__(self):
        if not printable_abs(self.workdir):
            raise ConfigError(f"workdir must be a printable absolute path, got {self.workdir!r:.80}")
        if self.tier is not None and (isinstance(self.tier, bool) or not isinstance(self.tier, int)
                                      or not 1 <= self.tier <= 4):
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort is not None and self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
        if self.access.cwd != "" and not printable_abs(self.access.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {self.access.cwd!r:.80}")
        if self.sid is not None and (error := sid_error(self.sid)):
            raise ConfigError(error)


@dataclass(frozen=True)
class Launch:
    """The command a run's launch gives the Driver."""
    argv: tuple[str, ...]                                  # argv[0] the runtime's executable
    env: Mapping[str, str] = field(default_factory=dict)   # added to the caller's environment
    unset: tuple[str, ...] = ()                            # removed from the caller's environment first
    cwd: str = ""          # "": spec.access.cwd, else spec.workdir
    transcript: str = ""   # where the runtime saves the session; "": unknown
    resume: str = ""       # the shell command a human resumes with, each interpolated field shlex.quoted; "": none

    def __post_init__(self):
        env = self.env
        for name, ok in (("argv", isinstance(self.argv, tuple) and all(map(_clean, self.argv))),
                         ("env", isinstance(env, Mapping) and all(_clean(k) and _clean(v) for k, v in env.items())),
                         ("unset", isinstance(self.unset, tuple) and all(map(_clean, self.unset)))):
            if not ok:
                raise ConfigError(f"launch: {name} must be strings without NUL")


@dataclass(frozen=True)
class SessionInfo:
    sid: str
    native_id: str   # the runtime's own session id; "" until bound
    cwd: str
    project: bool
    transcript: str
    resume: str


@dataclass(frozen=True)
class SessionBound:
    native_id: str


@dataclass(frozen=True)
class Activity:
    detail: str = ""


@dataclass(frozen=True)
class Reply:
    text: str


@dataclass(frozen=True)
class TurnEnd:
    """A turn's end: its reply and the count of background work still running. A headless run's final result."""
    reply: str = ""
    pending: int = 0


@dataclass(frozen=True)
class Blocked:
    reason: str = ""


@dataclass(frozen=True)
class LimitHit:
    """The runtime reported its usage limit; resets_at: when it resets, epoch seconds, None when not given."""
    resets_at: float | None = None


@dataclass(frozen=True)
class Exited:
    """The runtime's process ended; in headless mode only the Driver makes it."""
    returncode: int


Event = SessionBound | Activity | Reply | TurnEnd | Blocked | LimitHit | Exited

RESUMABLE = ("yes", "no", "unknown")


@dataclass(frozen=True)
class Window:
    """One usage limit window."""
    used_percent: float       # percent used, 0-100 (may exceed 100)
    resets_at: float | None   # when it resets, epoch seconds; None: not given


@dataclass(frozen=True)
class Usage:
    """A runtime account's usage; a window the runtime does not report is None."""
    five_hour: Window | None
    weekly: Window | None


@dataclass(frozen=True)
class SessionStatus:
    """What the runtime's own files say of a session."""
    resumable: str              # one of RESUMABLE
    last_active: float | None   # its newest write, epoch seconds; None: none found
    resume_command: str         # the shell command a human resumes it with; "": none


@dataclass(frozen=True)
class Stderr:
    """A diagnostic line, outside the frozen event set: recorded as `stderr` and passed up. The Driver makes one per
    stderr line of a headless run; an adapter may yield one too."""
    text: str


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Key:
    name: str   # a named key, e.g. "Enter"


class Adapter(ABC):
    """One runtime's abstract factory; a subclass per runtime."""
    keys: frozenset[str] = frozenset()   # its own table keys
    runtime_note: str = ""               # the Runtime note

    def __init__(self, config: Mapping):
        if extra := sorted(set(config) - self.keys):
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = dict(config)

    @abstractmethod
    def headless_run(self) -> "HeadlessRun | None":
        """A fresh HeadlessRun per call; None: headless mode unsupported."""

    @abstractmethod
    def tui_run(self) -> "TuiRun | None":
        """The runtime's TuiRun; None: tui mode unsupported."""

    def usage_probe(self) -> Launch | None:
        """The command of a short headless runtime call whose stdout carries the account's usage (the Driver runs it
        and gives its stdout to usage()); None: no source. A stdout-based source; step 4 (Codex) may revisit."""
        return None

    def usage(self, output: str) -> Usage | None:
        """The probe's stdout as Usage; None only when the probe failed (no reading in it). Never raises on bad
        output."""
        return None

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        """Session `sid` started in `cwd`, from the runtime's files; never raises on a file error."""
        return SessionStatus("unknown", None, "")


class HeadlessRun(ABC):
    """One headless session's runtime side, so launch may keep state for parse."""

    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, spec: Spec, resume: bool) -> Launch:
        """The command; spec.sid is always set, and resume True continues that session."""

    @abstractmethod
    def parse(self, record: dict) -> Iterable[Event | Stderr]:
        """One JSONL object's events, 0..n."""

    def events(self, lines: Iterable[str]) -> Iterator[Event | Stderr]:
        """parse() of each line that is a JSON object; any other line is skipped (a runtime overrides this to report
        it)."""
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield from self.parse(record)


class TuiRun(ABC):
    """One tui session's runtime side: its command, its submissions and its hook calls as events."""
    dialogs: tuple[str, ...] = ()   # screen texts of blocking dialogs that fire no hook

    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, spec: Spec, resume: bool, hook: tuple[str, ...]) -> Launch:
        """As HeadlessRun.launch; argv carries the runtime's hooks, each running the `hook` argv (installing them is the
        runtime's)."""

    @abstractmethod
    def reply(self, payload: dict) -> str:
        """The turn's reply from a Stop hook's payload."""

    def pending(self, payload: dict) -> int:
        """The background work still running at a Stop hook."""
        return 0

    def submit(self, text: str) -> tuple[Paste | Key, ...]:
        return Paste(text), Key("Enter")

    def hook_events(self, name: str, payload: dict, bound: str = "") -> list[Event]:
        """Hook `name`'s events: SessionBound first when payload's session_id is a non-empty str other than `bound`;
        then Stop → TurnEnd, PostToolUse → Activity(its tool_name), any other hook nothing more."""
        found = []
        native = payload.get("session_id")
        if isinstance(native, str) and native and native != bound:
            found.append(SessionBound(native))
        if name == "Stop":
            found.append(TurnEnd(self.reply(payload), self.pending(payload)))
        elif name == "PostToolUse":
            tool = payload.get("tool_name")
            found.append(Activity(tool if isinstance(tool, str) else ""))
        return found
