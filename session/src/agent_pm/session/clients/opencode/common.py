"""OpenCode's config checks and session bindings: each run's sid, under its cwd, bound to the
session id (`ses_…`) OpenCode chose, which a resume selects.
"""

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping

from agent_pm.session.clients.common import (
    CHECKS,
    FLAGS,
    MODES,
    check_config,
    mode_flags,
    scan,
)
from agent_pm.session.clients.opencode import probe
from agent_pm.session.clients.opencode.transcript import NATIVE, Refused, database
from agent_pm.session.clients.opencode.transcript import updated as session_updated
from agent_pm.session.engine.types import (
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    Effort,
    Launch,
    Permission,
    Resumable,
    SessionStatus,
    Spec,
    Usage,
)
from agent_pm.utils import (
    LOCAL_ENV_VARS,
    STATE,
    GitError,
    drop_stale,
    read_beneath,
    run_git,
    save,
    sh,
)

TABLE = "[clients.opencode]"
# A container Host's session state dir: OpenCode keeps opencode.db under $XDG_DATA_HOME/opencode.
HOME = ClientHome("opencode_dir", "opencode", "XDG_DATA_HOME", "none")
HERE = os.path.dirname(os.path.abspath(__file__))
STORE = f"{STATE}/sessions/opencode"  # under HOME, resolved per call
BINDING_KEYS = frozenset({"version", "cwd", "sid", "native"})
BINDING_MAX = 4096
OLLAMA_PORT = 11434
# Set by OpenCode for its children (a nested start's are never its own).
IDENTITY = ("OPENCODE", "OPENCODE_PID", "OPENCODE_TERMINAL")
# Config OpenCode merges over or beside the launch's own OPENCODE_CONFIG_CONTENT.
OVERRIDES = (
    "OPENCODE_PERMISSION",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)
UNSET = (*sorted(LOCAL_ENV_VARS), *IDENTITY, *OVERRIDES)
SELECTORS = ("-s", "--session", "-c", "--continue", "--fork")
RESERVED = (*SELECTORS, "--format", "--attach", "--dir")
PERMISSIONS = ("--auto", "--yolo", "--dangerously-skip-permissions")
TUI_RESERVED = ("--prompt", "--pure")  # the client's prompt; --pure would skip its plugin
VALUED = (
    "-m",
    "--model",
    "--agent",
    "--variant",
    "--format",
    "-f",
    "--file",
    "--title",
    "--attach",
    "-p",
    "--password",
    "-u",
    "--username",
    "--dir",
    "--port",
    "--prompt",
    "-s",
    "--session",
)
EXECUTABLE = (
    "executable",
    lambda v: isinstance(v, str) and (v == "" or v.strip() != "") and v.isprintable(),
    "a printable command, or empty for opencode on PATH",
)
VERSION = re.compile(r"(\d+)\.\d+\.\d+")
VERSION_WAIT = 10  # seconds
MAJOR = 1  # the OpenCode release the client supports (TASK-695)
PORT = (
    "ollama_port",
    lambda v: type(v) is int and 0 < v < 65536,
    "an integer port 1–65535",
)


PROTECTED = (".git", ".opencode", "opencode.json", "opencode.jsonc")  # names no run edits
CASED = (".git",)  # also denied in every case: OpenCode's patterns are case-sensitive, macOS's not
GIT_WAIT = 10  # seconds


def _ancestors(path: str) -> list[str]:
    """`path`'s proper ancestors, nearest first, "/" last."""
    found = []
    while (parent := os.path.dirname(path)) != path:
        found.append(parent)
        path = parent
    return found


def _spellings(name: str) -> list[str]:
    if name not in CASED:
        return [name]
    return [
        "".join(c)
        for c in itertools.product(*(dict.fromkeys((ch.lower(), ch.upper())) for ch in name))
    ]


def _git(argv: list[str], timeout: int):
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_ENV_VARS}
    return sh(argv, timeout, env=env)


def worktree(cwd: str) -> str:
    """OpenCode's worktree for `cwd` (its core/src/git.ts): the toplevel of the nearest .git's dir;
    "/" without one or when git cannot read it.
    """
    found = next(
        (p for p in (cwd, *_ancestors(cwd)) if os.path.lexists(os.path.join(p, ".git"))), None
    )
    if found is None:
        return "/"
    try:
        run_git(found, "rev-parse", "--git-dir", "--git-common-dir", run=_git, timeout=GIT_WAIT)
    except (GitError, OSError, subprocess.SubprocessError):
        return "/"
    try:
        top = run_git(found, "rev-parse", "--show-toplevel", run=_git, timeout=GIT_WAIT)
    except (GitError, OSError, subprocess.SubprocessError):
        return found
    top = top.rstrip("\r\n")
    return os.path.realpath(os.path.join(found, top)) if top else found


def edit_rules(cwd: str, dirs: list[str], reads: list[str]) -> dict[str, str]:
    """OpenCode's edit rules: the last match wins, a deny even under --auto, on a path relative to
    `worktree`. Each path by its deepest grant (cwd and dirs allow, reads deny), else deny; never
    PROTECTED. One rule per level above the worktree, as "*" also matches "../".
    """
    for path in (cwd, *dirs, *reads):
        if "*" in path or "?" in path:
            raise ConfigError(f"{path!r}: a wildcard in a path the opencode client grants")
    top = worktree(cwd)
    grants = dict(
        sorted(
            [*((path, "allow") for path in (cwd, *dirs)), *((path, "deny") for path in reads)],
            key=lambda grant: len(_ancestors(grant[0])),
        )
    )
    lineage = _ancestors(top)

    def above(level: int) -> str:
        return next((grants[p] for p in lineage[level - 1 :] if p in grants), "deny")

    rules = {"*": grants.get(top) or above(1)}
    levels = range(1, len(lineage) + 1) if any(p in grants for p in lineage) else (1,)
    rules.update({"../" * level + "*": above(level) for level in levels})
    for path, action in grants.items():
        if path != top and path not in lineage:
            rules[f"{os.path.relpath(path, top)}/*"] = action
    for name in PROTECTED:
        for spelled in _spellings(name):
            rules.update(
                dict.fromkeys((spelled, f"{spelled}/*", f"*/{spelled}", f"*/{spelled}/*"), "deny")
            )
    return rules


def image_path(found: Container, local: str) -> str:
    """`local`, a file of this package, at its path in the container's image."""
    package = __name__.split(".")[:-1]
    return os.path.join(found.source, *package, os.path.relpath(os.path.abspath(local), HERE))


def refuse(flags: Iterable[str], banned: tuple[str, ...], where: str) -> None:
    for flag in scan(tuple(flags), {word: word for word in VALUED}):
        # yargs also takes an option's camelCase spelling.
        kebab = re.sub(r"[A-Z]", lambda m: f"-{m.group().lower()}", flag.word)
        if flag.word in banned or kebab in banned:
            raise ConfigError(f"{flag.arg}: set by the opencode client, not {where}")


def refuse_env(env: Mapping[str, str], where: str) -> None:
    if found := sorted(set(env) & set(OVERRIDES)):
        raise ConfigError(f"{where}: {found[0]} would override the opencode client's config")


class OpenCodeCommon:
    def __init__(self, config: Mapping, *, selector):
        self.selector = selector
        check_config(config, TABLE, (FLAGS, MODES, PORT, EXECUTABLE, *CHECKS))
        self.executable = config.get("executable") or "opencode"
        self.flags = tuple(config.get("flags", ()))
        self.modes = {p: mode_flags(config, p) for p in Permission}
        refuse(self.flags, (*RESERVED, *PERMISSIONS), f"{TABLE}.flags")
        for permission, flags in self.modes.items():
            refuse(flags, RESERVED, f"{TABLE}.modes.{permission}")
        self.port = config.get("ollama_port", OLLAMA_PORT)
        self.tiers = dict(config.get("tiers", {}))
        self.efforts = dict(config.get("efforts", {}))
        self.env = dict(config.get("env", {}))
        refuse_env(self.env, f"{TABLE}.env")

    def _mapped(self, table: Mapping[str, str], key: str | None, missing: str) -> str | None:
        if key is None:
            return None
        if (found := table.get(key)) is None:
            raise ConfigError(f"{missing} in {TABLE}")
        return found

    def model(self, tier: int | None) -> str | None:
        tier_key = None if tier is None else str(tier)
        return self._mapped(self.tiers, tier_key, f"no model for tier {tier}")

    def variant(self, effort: Effort | None) -> str | None:
        return self._mapped(self.efforts, effort, f"no variant for effort {str(effort)!r}")

    def selection(
        self, spec: Spec, context: ClientContext, extra: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """A launch's checks and its session selection: on resume, (cwd, sid)'s bound session."""
        refuse(spec.flags, (*RESERVED, *PERMISSIONS, *extra), "a run's flags")
        if not spec.resume:
            return ()
        sid = spec.sid or ""
        native = self.native(spec.access.cwd, sid)
        if native is None:
            raise ConfigError(f"no OpenCode session bound to run ID {sid!r}")
        return self.selector(native, resume=True)

    @staticmethod
    def home() -> ClientHome:
        return HOME

    def executable_for(self, env: Mapping[str, str], cwd: str, context: ClientContext) -> str:
        """A container's image opencode (the broker checks its version), else program's."""
        if (found := context.container) is not None:
            return os.path.join(found.bin_dir, "opencode")
        return self.program(env, cwd)

    @staticmethod
    def transcript(env: Mapping[str, str], context: ClientContext) -> str:
        """A container launch's opencode.db, which its Host checks before a resume; else ""."""
        return database(env) if context.container is not None else ""

    def program(self, env: Mapping[str, str], cwd: str) -> str:
        """The executable, once its --version, run as the launch would run it, is OpenCode 1.x."""
        exe, fix = self.executable, f"set {TABLE} executable to an OpenCode {MAJOR}.x"
        run = {k: v for k, v in {**os.environ, **env}.items() if k not in LOCAL_ENV_VARS}
        try:
            done = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=VERSION_WAIT,
                stdin=subprocess.DEVNULL,
                env=run,
                cwd=cwd
                if os.path.isdir(cwd)
                else None,  # a run's cwd may not exist before its start
            )
        except OSError as error:
            raise ConfigError(
                f"opencode executable {exe!r}: {error.strerror or error}: {fix}"
            ) from None
        except subprocess.TimeoutExpired:
            raise ConfigError(f"opencode executable {exe!r}: --version timed out: {fix}") from None
        found = VERSION.search(done.stdout or "") if done.returncode == 0 else None
        if found is None:
            raise ConfigError(f"opencode executable {exe!r}: --version printed no version: {fix}")
        if int(found[1]) != MAJOR:
            raise ConfigError(f"opencode {found[0]} ({exe}) is not {MAJOR}.x: {fix}")
        return exe

    def inline(self, spec: Spec, host_address: str, plugin: str | None = None) -> dict:
        """The launch's OpenCode config: ollama on `host_address`, no auto-update, shell calls asking
        but pre-approved commands, Access dirs and reads outside the cwd, edit_rules.
        """
        access = spec.access
        models = {
            model.removeprefix("ollama/"): {"name": model.removeprefix("ollama/")}
            for model in self.tiers.values()
            if model.startswith("ollama/")
        }
        commands = dict.fromkeys(access.commands, "allow")
        permission: dict = {
            "bash": {"*": "ask", **commands} if commands else "ask",
        }
        dirs = [os.path.realpath(d) for d in access.dirs]
        reads = [os.path.realpath(d) for d in access.reads]
        if outside := [*reads, *dirs]:
            permission["external_directory"] = {
                pattern: "allow" for d in outside for pattern in (d, f"{d}/*")
            }
        permission["edit"] = edit_rules(os.path.realpath(access.cwd), dirs, reads)
        return {
            "$schema": "https://opencode.ai/config.json",
            **({"plugin": [plugin]} if plugin is not None else {}),
            "autoupdate": False,
            # A refused call ends OpenCode's turn otherwise (session/processor.ts).
            "experimental": {"continue_loop_on_deny": True},
            "provider": {
                "ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Ollama",
                    "options": {"baseURL": f"http://{host_address}:{self.port}/v1"},
                    "models": models,
                }
            },
            "permission": permission,
        }

    def environment(
        self, spec: Spec, context: ClientContext, plugin: str | None = None
    ) -> dict[str, str]:
        """The config's env, then Access's and the Spec's, under the client's own."""
        access = spec.access
        supplied = {**access.env, **spec.env}
        refuse_env(supplied, "a run's env")
        env = {k: v for k, v in {**self.env, **supplied}.items() if k not in LOCAL_ENV_VARS}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.inline(spec, context.host_address, plugin))
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        if not access.project:
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        return env

    def probe(self) -> Launch:
        """On this machine, so the loopback, never the Host's address."""
        return Launch(
            (os.path.realpath(sys.executable), "-I", probe.__file__, str(self.port)),
            env={k: v for k, v in self.env.items() if k not in LOCAL_ENV_VARS},
            unset=UNSET,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """A model listing: Ollama is up, and a local model has no limits; anything else unknown."""
        try:
            found = json.loads(output)
        except (ValueError, RecursionError):
            return None
        if isinstance(found, dict) and isinstance(found.get("models"), list):
            return Usage(None, None)
        return None

    @property
    def store(self) -> str:
        return os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))

    def bind(self, cwd: str, sid: str, native: str) -> None:
        Bindings(self.store).bind(cwd, sid, native)

    def native(self, cwd: str, sid: str) -> str | None:
        return Bindings(self.store).native(cwd, sid)

    def unbind(self, cwd: str, sid: str) -> None:
        Bindings(self.store).unbind(cwd, sid)

    def session_status(
        self, cwd: str, sid: str, env: Mapping[str, str], *, transcript: str = ""
    ) -> SessionStatus:
        """From the bound session's row in opencode.db: the run's `transcript`, else the db the
        run's opencode would find.
        """
        try:
            native = self.native(cwd, sid)
        except ConfigError:
            return SessionStatus(Resumable.UNKNOWN, None, "")
        if native is None:
            return SessionStatus(Resumable.NO, None, "")
        db = transcript or database({**os.environ, **self.env, **env})
        try:
            updated = session_updated(db, native)
        except Refused:
            return SessionStatus(Resumable.UNKNOWN, None, "")
        if updated is None:
            return SessionStatus(Resumable.NO, None, "")
        return SessionStatus(Resumable.YES, updated, "")


class Homed:
    """The Docker home both OpenCode adapters name."""

    common: OpenCodeCommon

    def _home(self) -> ClientHome:
        return self.common.home()


class Bindings:
    """Each run's (cwd, sid) bound to its OpenCode session, one file each under `store`."""

    def __init__(self, store: str):
        self.store = store

    def _binding(self, cwd: str, sid: str) -> str:
        key = hashlib.sha256(f"{os.path.realpath(cwd)}\0{sid}".encode()).hexdigest()
        return os.path.join(self.store, f"{key}.json")

    def bind(self, cwd: str, sid: str, native: str) -> None:
        if not NATIVE.fullmatch(native):
            raise ConfigError(f"invalid OpenCode session id {native!r:.80}")
        record = {"version": 1, "cwd": os.path.realpath(cwd), "sid": sid, "native": native}
        try:
            os.makedirs(self.store, 0o700, exist_ok=True)
            save(self._binding(cwd, sid), json.dumps(record), durable=True)
        except OSError as error:
            raise ConfigError(f"opencode session binding: {error}") from None
        for error in drop_stale(self.store):
            print(f"opencode session binding: {error}", file=sys.stderr)

    def unbind(self, cwd: str, sid: str) -> None:
        try:
            os.unlink(self._binding(cwd, sid))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ConfigError(f"opencode session binding: {error}") from None

    def native(self, cwd: str, sid: str) -> str | None:
        """The session bound to (cwd, sid); None when none is (a symlink counts as none)."""
        path = self._binding(cwd, sid)
        data = read_beneath(self.store, path, limit=BINDING_MAX + 1)
        if data is None:
            return None
        try:
            record = json.loads(data) if len(data) <= BINDING_MAX else None
        except (ValueError, RecursionError):
            record = None
        if (
            not isinstance(record, dict)
            or frozenset(record) != BINDING_KEYS
            or record["version"] != 1
            or record["cwd"] != os.path.realpath(cwd)
            or record["sid"] != sid
            or not isinstance(record["native"], str)
            or not NATIVE.fullmatch(record["native"])
        ):
            raise ConfigError(f"{path}: not an opencode session binding for run ID {sid!r}")
        return record["native"]
