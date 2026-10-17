"""The root-owned broker, protocol 1 (docs/docker-broker-protocol.md): version, build, start and close
with strict response parsing. A missing, malformed or inconsistent response is a transport failure;
a valid error response is a BrokerError.
"""

import json
import os
import re
import subprocess
from dataclasses import fields

from agent_pm.session.engine.types import ConfigError, SessionError
from agent_pm.session.hosts.docker.types import Started
from agent_pm.utils import save

# Step 1 (TASK-559) installs BROKER and, in the image, RELAY and the hook stub (docs/docker-host.md).
BROKER = "/usr/local/libexec/agent-pm/agent-pm-broker"
RELAY = "/opt/agent-pm/bin/relay"
PROTOCOL = "1"
SCHEMA = "protocol/docker-broker/protocol.schema.json"
INSTALL = "install or upgrade it: see docs/docker-host.md#install"
TIMEOUT = 1800

TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
IMAGE = re.compile(r"agent-pm-worker:[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER = re.compile(r"agent-pm-[A-Za-z0-9_-]+-[0-9a-f]{32}")
ENDPOINT = re.compile(r"unix:///[^\x00-\x1f\x7f]+/\.docker/run/docker\.sock")
REASONS = frozenset({"network", "timeout", "command_failed", "version_failed"})
COMMANDS = frozenset({"version", "build", "start", "close", "unknown"})
CODES = {  # code → (exit code, the commands that report it)
    "usage": (2, COMMANDS),
    "manifest_rejected": (3, {"start"}),
    "mount_rejected": (4, {"start"}),
    "login_required": (5, {"start"}),
    "image_rejected": (6, {"start"}),
    "docker_unreachable": (7, {"build", "start", "close"}),
    "build_failed": (8, {"build"}),
    "start_failed": (9, {"start"}),
    "close_failed": (10, {"close"}),
    "keychain_failed": (11, {"start"}),
    "internal_error": (70, COMMANDS),
}
ERROR_KEYS = frozenset({"command", "code", "message", "cleanup"})
ERROR_OPTIONAL = frozenset({"cause", "cleanup_error", "container"})


class BrokerError(SessionError):
    def __init__(
        self, exit_code: int, code: str, message: str, cleanup: str, container: str | None
    ) -> None:
        self.exit_code, self.code, self.cleanup, self.container = (
            exit_code,
            code,
            cleanup,
            container,
        )
        super().__init__(f"broker: {message}")


class _Bad(Exception):
    pass


def _unique(pairs):
    found = dict(pairs)
    if len(found) != len(pairs):
        raise _Bad("a duplicate key")
    return found


def _constant(name):
    raise _Bad(f"constant {name}")


def _object(text: str) -> dict:
    try:
        found = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise _Bad("not one JSON object") from error
    if not isinstance(found, dict):
        raise _Bad("not a JSON object")
    return found


def _text(value) -> bool:
    return isinstance(value, str) and bool(TEXT.fullmatch(value))


def _matches(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _error(command: str, returncode: int, data: dict) -> BrokerError:
    error, exit_code = data.get("error"), data.get("exit_code")
    if not (
        set(data) == {"exit_code", "error"}
        and type(exit_code) is int
        and exit_code == returncode
        and isinstance(error, dict)
        and ERROR_KEYS <= set(error) <= ERROR_KEYS | ERROR_OPTIONAL
        and all(isinstance(error[key], str) for key in ERROR_KEYS)
    ):
        raise _Bad("error shape")
    code, cleanup, container = error["code"], error["cleanup"], error.get("container")
    if not (
        CODES.get(code, (None,))[0] == exit_code
        and error["command"] in CODES[code][1]
        and error["command"] in {command, "unknown"}
        and cleanup in {"not_needed", "removed", "failed"}
        and (command == "start" or cleanup == "not_needed")
        and (cleanup == "failed") == ("cleanup_error" in error)
        and (cleanup != "failed" or container is not None)
        and (container is None or _matches(CONTAINER, container))
        and all(_text(error[key]) for key in ("message", "cause", "cleanup_error") if key in error)
    ):
        raise _Bad("error fields")
    return BrokerError(exit_code, code, error["message"], cleanup, container)


def _update(update) -> bool:
    if not isinstance(update, dict):
        return False
    if update == {"status": "updated"}:
        return True
    return (
        set(update) == {"status", "reason", "message"}
        and update["status"] == "fallback"
        and isinstance(update["reason"], str)
        and update["reason"] in REASONS
        and _text(update["message"])
    )


class Broker:
    def __init__(self, runner=subprocess.run) -> None:
        self.runner = runner

    def _run(self, command: str, *args: str) -> dict:
        try:
            res = self.runner(
                [BROKER, command, *args],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env={"PATH": os.defpath},
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise SessionError(f"broker: {command} timed out after {TIMEOUT} s") from None
        except UnicodeDecodeError:
            raise SessionError(f"broker: bad response to {command}: not UTF-8") from None
        except OSError as error:
            raise SessionError(f"broker {BROKER}: {error.strerror}; {INSTALL}") from None
        try:
            if res.returncode == 0:
                return _object(res.stdout)
            raise _error(command, res.returncode, _object(res.stderr))
        except _Bad as why:
            raise SessionError(f"broker: bad response to {command}: {why}") from None

    def version(self) -> None:
        found = self._parsed("version", lambda d: set(d) == {"protocol_version"})
        if found["protocol_version"] != PROTOCOL:
            raise SessionError(
                f"broker {BROKER}: protocol {found['protocol_version']!r:.20}, want {PROTOCOL!r};"
                f" {INSTALL}"
            )

    def build(self, package: str) -> str:
        """The image tag built for `package`."""
        found = self._parsed(
            "build",
            lambda d: (
                set(d) == {"image", "digest", "rebuilt"}
                and _matches(IMAGE, d["image"])
                and _matches(DIGEST, d["digest"])
                and type(d["rebuilt"]) is bool
            ),
            "--agent-pm",
            package,
        )
        return found["image"]

    def start(self, manifest: str, *, clients: frozenset[str]) -> Started:
        """`clients`: the client names the protocol requires in the response."""
        found = self._parsed(
            "start", lambda d: set(d) == {f.name for f in fields(Started)}, "--manifest", manifest
        )
        started = Started(**found)
        if not (
            _matches(CONTAINER, started.container)
            and _matches(ENDPOINT, started.endpoint)
            and _matches(DIGEST, started.image_digest)
            and isinstance(started.clients, dict)
            and started.clients.keys() == clients
            and all(map(_text, started.clients.values()))
            and _update(started.update)
        ):
            raise SessionError("broker: bad response to start: fields")
        return started

    def close(self, container: str) -> bool:
        """Whether it removed `container`; False: it was already gone."""
        found = self._parsed(
            "close",
            lambda d: (
                d.keys() == {"container", "removed"}
                and d["container"] == container
                and type(d["removed"]) is bool
            ),
            "--container",
            container,
        )
        return found["removed"]

    def _parsed(self, command: str, valid, *args: str) -> dict:
        found = self._run(command, *args)
        if not valid(found):
            raise SessionError(f"broker: bad response to {command}: fields")
        return found


def plugin_root(hook_path: str) -> str:
    """The running plugin's root: the parent of the bin/ holding its agent-pm-session."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(hook_path)))
    if not os.path.isfile(os.path.join(root, SCHEMA)):
        raise ConfigError(f"agent-pm plugin {root}: no {SCHEMA}")
    return root


def client_keys(root: str) -> frozenset[str]:
    """The client names a start response must report: the required keys of start_output's one
    inline object property in the protocol schema under `root`.
    """
    try:
        with open(os.path.join(root, SCHEMA), encoding="utf-8") as f:
            properties = json.load(f)["$defs"]["start_output"]["properties"]
        (keys,) = (
            p["required"]
            for p in properties.values()
            if isinstance(p, dict) and p.get("type") == "object" and "required" in p
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f"{SCHEMA}: no start_output client map ({error})") from None
    return frozenset(keys)


def write_manifest(
    path: str,
    *,
    name: str,
    generation: str,
    image: str,
    cwd: str,
    home: tuple[str, str],
    hook_path: str,
) -> None:
    """The start manifest, 0600; home: (its manifest field, the client home's path)."""
    field, home_path = home
    manifest = {
        "name": name,
        "generation": generation,
        "image": image,
        "cwd": cwd,
        field: home_path,
        "hook_path": hook_path,
        "labels": {},
    }
    save(path, json.dumps(manifest), durable=True)
