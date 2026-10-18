import argparse
import base64
import fcntl
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

VERSION = "0.163.0"
CONTAINERS = ("task647-codex-a-1", "task647-codex-a-2")
STORE = 'cli_auth_credentials_store="file"'
ITEM_TYPES = (
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
    "error",
)


@contextmanager
def private_directory(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("auth_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("private_directory")
        yield parent
    finally:
        os.close(parent)


def private_regular(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError("private_file")


def read_private_auth(path):
    with private_directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as file:
            private_regular(file.fileno())
            data = file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("auth_size")
    return json.loads(data)


@contextmanager
def refresh_lock(directory):
    with private_directory(directory) as parent:
        fd = os.open(
            "task647-refresh.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        try:
            private_regular(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


def make_carrier(auth, variant):
    if (
        variant not in ("missing-refresh", "empty-refresh", "external-access")
        or auth.get("auth_mode") != "chatgpt"
    ):
        raise ValueError("auth_mode_or_variant")
    tokens = auth["tokens"]
    if any(
        not isinstance(tokens.get(k), str) or not tokens[k]
        for k in ("id_token", "access_token", "refresh_token")
    ):
        raise ValueError("managed_tokens")
    selected = {key: tokens[key] for key in ("id_token", "access_token")}
    account = tokens.get("account_id")
    if account is not None and not isinstance(account, str):
        raise ValueError("account_type")
    selected["account_id"] = account
    if variant in ("empty-refresh", "external-access"):
        selected["refresh_token"] = ""
    result = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": selected}
    if "last_refresh" in auth:
        if auth["last_refresh"] is not None and not isinstance(auth["last_refresh"], str):
            raise ValueError("refresh_date_type")
        result["last_refresh"] = auth["last_refresh"]
    if variant == "external-access":
        if not account:
            raise ValueError("external_account")
        refresh_date(result.get("last_refresh"))
        selected["id_token"] = tokens["access_token"]
        result["auth_mode"] = "chatgptAuthTokens"
    values = [*selected.values(), result.get("last_refresh")]
    if any(tokens["refresh_token"] in value for value in values if isinstance(value, str)):
        raise ValueError("refresh_material")
    return result


def checked_container(container):
    if container not in CONTAINERS:
        raise ValueError("container")
    return container


def docker(argv, *, data=None):
    return subprocess.run(argv, input=data, capture_output=True, check=False, timeout=180)


def inject(container, auth, variant):
    payload = json.dumps(make_carrier(auth, variant)).encode()
    result = docker(
        [
            "docker",
            "exec",
            "-i",
            checked_container(container),
            "sh",
            "-eu",
            "-c",
            "umask 077; cat > /codex/auth.next; chmod 0400 /codex/auth.next; "
            "mv /codex/auth.next /codex/auth.json",
        ],
        data=payload,
    )
    return {
        "probe": variant,
        "status": "injected" if result.returncode == 0 else "incomplete",
        "exit_code": result.returncode,
        "refresh_value_count": 0,
    }


def exec_command(container):
    return [
        "docker",
        "exec",
        checked_container(container),
        "codex",
        "-c",
        STORE,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "Reply exactly OK. Do not use tools.",
    ]


ERROR_ITEM_TEMPLATES = (
    (
        "diagnostic_log_write",
        "app-server/src/log_write_warning.rs:14",
        re.escape(
            "Codex couldn't save diagnostic logs to its local database. "
            "Run `codex doctor` for diagnostics."
        ),
    ),
    (
        "diagnostic_log_write",
        "app-server/src/log_write_warning.rs:14",
        re.escape(
            "Codex couldn't save diagnostic logs to its local database. "
            "Use /feedback with logs included before closing Codex, or run `codex doctor` for diagnostics."
        ),
    ),
    (
        "under_development_features",
        "features/src/lib.rs:2136",
        r"Under-development features enabled: .+\. Under-development features are incomplete "
        r"and may behave unpredictably\. To suppress this warning, set "
        r"`suppress_unstable_features_warning = true` in .+\.",
    ),
    (
        "model_metadata_fallback",
        "core/src/session/turn_context.rs:1401",
        r"Model metadata for `[^`]+` not found\. Defaulting to fallback metadata; "
        r"this can degrade performance and cause issues\.",
    ),
    (
        "transport_fallback",
        "core/src/responses_retry.rs:133",
        r"Falling back from WebSockets to HTTPS transport\. .+",
    ),
    (
        "event_stream_lag",
        "exec/src/lib.rs:1668",
        r"in-process app-server event stream lagged; dropped [0-9]+ events",
    ),
    (
        "model_rerouted",
        "exec/src/event_processor_with_jsonl_output.rs:499",
        r"model rerouted: .+ -> .+ \(.+\)",
    ),
    (
        "credential_masking_sandbox_disabled",
        "core/src/config/mod.rs:4876",
        re.escape(
            "Credential masking is inactive because sandboxing is disabled. Commands will receive "
            "unmasked credentials. Select a sandboxed permission profile to use credential masking."
        ),
    ),
    (
        "credential_masking_key_conflict",
        "core/src/config/mod.rs:4894",
        re.escape(
            "Credential masking is inactive because shell environment overrides contain conflicting "
            "case-insensitive provider keys. Commands will receive unmasked credentials. Remove the "
            "conflicting entries from `shell_environment_policy.set`."
        ),
    ),
    (
        "credential_masking_proxy_disabled",
        "core/src/config/mod.rs:4898",
        re.escape(
            "Credential masking is inactive because the network proxy is disabled. Commands will "
            "receive unmasked credentials. Enable `features.network_proxy` or configure managed "
            "network requirements to use credential masking."
        ),
    ),
    (
        "hook_trust_bypassed",
        "core/src/config/mod.rs:3431",
        re.escape(
            "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review "
            "for this invocation."
        ),
    ),
    (
        "requirements_value_fallback",
        "core/src/config/mod.rs:2318",
        r"Configured value for `[^`]+` is disallowed by requirements; falling back to required "
        r"value .+\. Details: .+",
    ),
    (
        "requirements_profile_fallback",
        "core/src/config/mod.rs:5025",
        r"Configured value for `permission_profile` is disallowed by requirements; falling back "
        r"from `[^`]+` to required value `[^`]+`\.",
    ),
    (
        "execpolicy_parse",
        "app-server/src/in_process.rs:381",
        r"Error parsing rules; custom rules not applied\.(?: \(.+\))?",
    ),
    (
        "code_mode_unavailable",
        "core/src/tools/code_mode/mod.rs:120",
        r"Code Mode is unavailable because .+\. "
        r"(?:Falling back to direct tools|Code mode will fail closed); "
        r"enable `features\.code_mode_host` and install `codex-code-mode-host`\.",
    ),
    (
        "code_mode_unsupported",
        "core/src/session/code_mode_warning.rs:20",
        r"Code Mode is enabled in configuration, but model `[^`]+` does not advertise Code Mode "
        r"support\. This may degrade model performance\. Disable `features\.code_mode` and "
        r"`features\.code_mode_only`, or select a model whose metadata enables Code Mode\.",
    ),
    (
        "transcript_save_retry",
        "core/src/tasks/mod.rs:395",
        r"Failed to save the conversation transcript; Codex will continue retrying\. Error: .+",
    ),
    (
        "compaction_accuracy",
        "core/src/compact.rs:432",
        re.escape(
            "Heads up: Long threads and multiple compactions can cause the model to be less "
            "accurate. Start a new thread when possible to keep threads small and targeted."
        ),
    ),
    (
        "resume_model_changed",
        "core/src/session/mod.rs:1699",
        r"This session was recorded with model `([^`]+)` but is resuming with `[^`]+`\. "
        r"Consider switching back to `\1` as it may affect Codex performance\.",
    ),
    (
        "service_tier_unsupported",
        "core/src/session/mod.rs:1219",
        r"Configured service tier `[^`]+` is not advertised as supported for model `[^`]+` "
        r"and will be omitted from requests\.",
    ),
)


def error_item_category(message):
    if not isinstance(message, str) or len(message) > 64 * 1024:
        return "unknown"
    matches = {
        category
        for category, _source, pattern in ERROR_ITEM_TEMPLATES
        if re.fullmatch(pattern, message, re.DOTALL)
    }
    return matches.pop() if len(matches) == 1 else "unknown"


def summarize_turn(output, code):
    result = {
        "status": "incomplete",
        "exit_code": code,
        "completed_turn_count": None,
        "ok_reply_count": None,
        "last_reply_ok": None,
        "error_event_count": None,
        "rejected_item_type_counts": None,
        "error_item_category_counts": None,
        "error": "redacted_or_incomplete",
    }
    if len(output) > 4 * 1024 * 1024:
        return result
    try:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        completed = sum(event.get("type") == "turn.completed" for event in events)
        replies = [
            event["item"].get("text", "")
            for event in events
            if event.get("type") == "item.completed"
            and event.get("item", {}).get("type") == "agent_message"
        ]
        bad = any(event.get("type") in ("turn.failed", "error") for event in events)
        tools = any(
            event.get("item", {}).get("type") not in (None, "agent_message", "reasoning")
            for event in events
        )
        rejected = {}
        categories = {}
        for event in events:
            kind = event.get("item", {}).get("type")
            if kind not in (None, "agent_message", "reasoning"):
                label = kind if isinstance(kind, str) and kind in ITEM_TYPES else "other"
                rejected[label] = rejected.get(label, 0) + 1
            if kind == "error":
                category = error_item_category(event["item"].get("message"))
                categories[category] = categories.get(category, 0) + 1
        result.update(
            completed_turn_count=completed,
            ok_reply_count=sum(text.strip() == "OK" for text in replies),
            last_reply_ok=bool(replies) and replies[-1].strip() == "OK",
            error_event_count=sum(
                event.get("type") in ("error", "turn.failed") for event in events
            ),
            rejected_item_type_counts=rejected,
            error_item_category_counts=categories,
        )
        if (
            code == 0
            and completed == 1
            and replies
            and replies[-1].strip() == "OK"
            and not bad
            and not tools
        ):
            result.update(status="pass", completed_turn_count=1, ok_reply_count=1, error=None)
    except (ValueError, AttributeError, TypeError, KeyError):
        pass
    return result


def now():
    return datetime.now(UTC).isoformat()


def observed_version(container):
    result = docker(["docker", "exec", checked_container(container), "codex", "--version"])
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not re.fullmatch(r"codex(?:-cli)? 0\.163\.0", text):
        raise ValueError("runtime_version")
    return VERSION


def turn(container):
    version = observed_version(container)
    started = now()
    result = docker(exec_command(container))
    return {
        "container": container,
        "version": version,
        "started_at": started,
        "ended_at": now(),
        **summarize_turn(result.stdout, result.returncode),
    }


def summarize_pair(observations):
    overlap = max(datetime.fromisoformat(row["started_at"]) for row in observations) < min(
        datetime.fromisoformat(row["ended_at"]) for row in observations
    )
    passed = overlap and all(row["status"] == "pass" for row in observations)
    return {
        "probe": "concurrent_exec",
        "status": "pass" if passed else "incomplete",
        "overlap": overlap,
        "count": len(observations),
        "observations": observations,
    }


def access_expiry(auth, observed_at):
    if auth.get("auth_mode") != "chatgpt":
        raise ValueError("auth_mode")
    parts = auth["tokens"]["access_token"].split(".")
    if len(parts) != 3:
        raise ValueError("jwt_shape")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True))
    exp = claims["exp"]
    if type(exp) is not int:
        raise ValueError("expiry_type")
    issued = claims.get("iat")
    return {
        "probe": "host_access_expiry",
        "status": "observed",
        "access_expires_at": datetime.fromtimestamp(exp, UTC).isoformat(),
        "remaining_seconds": exp - int(observed_at.timestamp()),
        "declared_lifetime_seconds": exp - issued if type(issued) is int else None,
        "signature_status": "not_verified",
    }


def refresh_date(value):
    if not isinstance(value, str):
        raise ValueError("refresh_date")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("refresh_timezone")
    return stamp.astimezone(UTC)


def summarize_refresh(before, after, observed_at):
    result = {"status": "incomplete", "signature_status": "not_verified"}
    try:
        for auth in (before, after):
            if auth.get("auth_mode") != "chatgpt":
                raise ValueError("managed_auth")
            tokens = auth["tokens"]
            if any(
                not isinstance(tokens.get(key), str) or not tokens[key]
                for key in ("id_token", "access_token", "refresh_token", "account_id")
            ):
                raise ValueError("managed_tokens")
        old_expiry = refresh_date(access_expiry(before, observed_at)["access_expires_at"])
        expiry = refresh_date(access_expiry(after, observed_at)["access_expires_at"])
        old_refresh = refresh_date(before["last_refresh"])
        refreshed = refresh_date(after["last_refresh"])
        result.update(
            access_changed=before["tokens"]["access_token"] != after["tokens"]["access_token"],
            refresh_changed=before["tokens"]["refresh_token"] != after["tokens"]["refresh_token"],
            same_account=before["tokens"]["account_id"] == after["tokens"]["account_id"],
            expiry_advanced=expiry > old_expiry,
            expiry_future=expiry > observed_at,
            last_refresh_advanced=refreshed > old_refresh,
            before_access_expires_at=old_expiry.isoformat(),
            access_expires_at=expiry.isoformat(),
            before_last_refresh_at=old_refresh.isoformat(),
            last_refresh_at=refreshed.isoformat(),
        )
        if all(
            result[key]
            for key in (
                "access_changed",
                "same_account",
                "expiry_advanced",
                "expiry_future",
                "last_refresh_advanced",
            )
        ):
            result["status"] = "rotation_observed"
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
        pass
    return result


class BoundedLines:
    def __init__(self, line_limit=1024 * 1024, total_limit=4 * 1024 * 1024):
        self.line_limit = line_limit
        self.total_limit = total_limit
        self.total = 0
        self.pending = b""

    def feed(self, data):
        self.total += len(data)
        if self.total > self.total_limit:
            raise ValueError("output_limit")
        self.pending += data
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        if any(len(line) > self.line_limit for line in (*lines, self.pending)):
            raise ValueError("line_limit")
        return [json.loads(line) for line in lines if line.strip()]

    def finish(self):
        if self.pending.strip():
            raise ValueError("truncated_output")


class RefreshExchange:
    def __init__(self):
        self.stage = 1
        self.complete = False
        self.notification_count = 0
        self.request_count = 0

    def start(self):
        return (
            json.dumps(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "task647-probe", "title": None, "version": VERSION},
                        "capabilities": {
                            "experimentalApi": False,
                            "requestAttestation": False,
                            "explicitGatewayOauth": True,
                        },
                    },
                }
            ).encode()
            + b"\n"
        )

    def accept(self, message):
        if not isinstance(message, dict):
            raise ValueError("protocol_shape")
        if "method" in message:
            if "id" in message or not isinstance(message["method"], str):
                raise ValueError("unexpected_request")
            self.notification_count += 1
            return []
        if (
            "error" in message
            or type(message.get("id")) is not int
            or message["id"] != self.stage
            or self.complete
        ):
            raise ValueError("rpc_failure")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ValueError("rpc_result")
        if self.stage == 1:
            self.stage = 2
            self.request_count = 1
            return [
                b'{"method":"initialized"}\n',
                b'{"id":2,"method":"account/read","params":{"refreshToken":true}}\n',
            ]
        account = result.get("account")
        if (
            not isinstance(account, dict)
            or account.get("type") != "chatgpt"
            or result.get("requiresOpenaiAuth") is not True
        ):
            raise ValueError("managed_account_required")
        self.complete = True
        return []

    def summary(self):
        return {
            "proactive_request_count": self.request_count,
            "notification_count": self.notification_count,
            "account_read_completed": self.complete,
        }


class ReloadExchange(RefreshExchange):
    rpc_timeout = 300

    def __init__(self, before, rotate):
        super().__init__()
        self.before = before
        self.after = None
        self.rotate = rotate
        self.refresh = {"status": "not_started"}
        self.initial_cache_matched = False
        self.old_cache_retained_after_replace = False
        self.replacement_loaded = False
        self.started = None
        self.elapsed = None

    def auth_request(self):
        return (
            json.dumps(
                {
                    "id": self.stage,
                    "method": "getAuthStatus",
                    "params": {"includeToken": True, "refreshToken": False},
                }
            ).encode()
            + b"\n"
        )

    def accept(self, message):
        if not isinstance(message, dict):
            raise ValueError("protocol_shape")
        if "method" in message:
            if "id" in message or not isinstance(message["method"], str):
                raise ValueError("unexpected_request")
            self.notification_count += 1
            return []
        if (
            "error" in message
            or type(message.get("id")) is not int
            or message["id"] != self.stage
            or self.complete
            or not isinstance(message.get("result"), dict)
        ):
            raise ValueError("rpc_failure")
        result = message["result"]
        if self.stage == 1:
            self.stage = 2
            return [b'{"method":"initialized"}\n', self.auth_request()]
        if self.stage == 4:
            if (
                not isinstance(result.get("account"), dict)
                or result["account"].get("type") != "chatgpt"
                or result.get("requiresOpenaiAuth") is not True
            ):
                raise ValueError("managed_account_required")
            self.stage = 5
            return [self.auth_request()]
        expected = self.after if self.stage == 5 else self.before
        if (
            result.get("authMethod") != "chatgpt"
            or result.get("requiresOpenaiAuth") is not True
            or not isinstance(result.get("authToken"), str)
            or not result["authToken"]
            or result["authToken"] != expected["tokens"]["access_token"]
        ):
            raise ValueError("cached_token_mismatch")
        if self.stage == 2:
            self.initial_cache_matched = True
            self.started = time.monotonic()
            self.refresh, self.after = self.rotate()
            if (
                self.refresh.get("status") != "rotation_observed"
                or self.after.get("auth_mode") != "chatgpt"
                or self.after["tokens"]["account_id"] != self.before["tokens"]["account_id"]
                or self.after["tokens"]["access_token"] == self.before["tokens"]["access_token"]
            ):
                raise ValueError("replacement_rotation_required")
            self.stage = 3
            return [self.auth_request()]
        if self.stage == 3:
            self.old_cache_retained_after_replace = True
            self.request_count = 1
            self.stage = 4
            return [b'{"id":4,"method":"account/read","params":{"refreshToken":true}}\n']
        self.replacement_loaded = True
        self.elapsed = time.monotonic() - self.started
        if not math.isfinite(self.elapsed) or self.elapsed <= 0:
            raise ValueError("measurement_clock")
        self.complete = True
        return []

    def summary(self):
        return {
            "initial_cache_matched": self.initial_cache_matched,
            "old_cache_retained_after_replace": self.old_cache_retained_after_replace,
            "replacement_loaded": self.replacement_loaded,
            "next_rpc_id": None if self.complete else self.stage,
            "reload_request_count": self.request_count,
            "notification_count": self.notification_count,
            "refresh_reload_elapsed_seconds": round(self.elapsed, 6) if self.complete else None,
            "observed_refresh_lead_seconds": math.ceil(self.elapsed) if self.complete else None,
            "host_refresh": self.refresh,
        }


def stop_native(process):
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(KeyboardInterrupt):
        time.sleep(0.25)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def run_native(argv, environment, exchange=None, *, remote=False):
    process = None
    result = {"exit_code": None, "cleanup_status": "not_started", "error": "native_failure"}
    output = bytearray()
    try:
        process = subprocess.Popen(
            argv,
            env=environment,
            cwd=environment["HOME"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        started = time.monotonic()
        deadline = started + (getattr(exchange, "rpc_timeout", 120) if exchange else 10)
        decoder = BoundedLines() if exchange else None
        stderr_size = 0
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            if exchange:
                process.stdin.write(exchange.start())
                process.stdin.flush()
            else:
                process.stdin.close()
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("native_timeout")
                for key, _events in selector.select(timeout=min(0.2, deadline - time.monotonic())):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout" and decoder:
                            decoder.finish()
                        continue
                    if key.data == "stderr":
                        stderr_size += len(data)
                        if stderr_size > 1024 * 1024:
                            raise ValueError("stderr_limit")
                    elif decoder:
                        for message in decoder.feed(data):
                            for request in exchange.accept(message):
                                process.stdin.write(request)
                                process.stdin.flush()
                            if exchange.complete and not process.stdin.closed:
                                process.stdin.close()
                                deadline = time.monotonic() + 50
                    else:
                        output.extend(data)
                        if len(output) > 4096:
                            raise ValueError("version_limit")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            result.update(exit_code=code, cleanup_status="exited")
            if code != 0 or exchange and not exchange.complete:
                raise ValueError("native_exit")
            result["error"] = None
    except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError):
        pass
    except KeyboardInterrupt:
        result["error"] = "cancelled"
    finally:
        if process:
            if remote and process.returncode is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=50)
                    result["cleanup_status"] = "exited_after_eof"
                except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
                    result.update(cleanup_status="remote_exit_unconfirmed", error="cleanup_failure")
            if process.returncode is None:
                try:
                    stop_native(process)
                    if not remote:
                        result["cleanup_status"] = "terminated"
                except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
                    result.update(cleanup_status="cleanup_failed", error="cleanup_failure")
            result["exit_code"] = process.returncode
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
    return result, bytes(output)


def validate_native(root, environment):
    with private_directory(root) as parent:
        fd = os.open("codex", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            private_regular(fd)
            if not os.fstat(fd).st_mode & stat.S_IXUSR:
                raise ValueError("native_mode")
        finally:
            os.close(fd)
    result, output = run_native([str(root / "codex"), "-c", STORE, "--version"], environment)
    if result["error"] or not re.fullmatch(rb"codex(?:-cli)? 0\.163\.0\s*", output):
        raise ValueError("native_version")


def host_refresh(root):
    result = {"probe": "host_refresh", "version": VERSION, "status": "incomplete"}
    environment = {
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "broker"),
        "PATH": "/usr/bin:/bin",
    }
    try:
        validate_native(root, environment)
        with private_directory(root / "home"):
            pass
        with refresh_lock(root / "broker"):
            before = read_private_auth(root / "broker/auth.json")
            if summarize_refresh(before, before, datetime.now(UTC)).get("same_account") is not True:
                raise ValueError("managed_auth")
            exchange = RefreshExchange()
            result["started_at"] = now()
            native, _output = run_native(
                [str(root / "codex"), "-c", STORE, "app-server", "--listen", "stdio://"],
                environment,
                exchange,
            )
            result.update(native, **exchange.summary(), ended_at=now())
            after = read_private_auth(root / "broker/auth.json")
            observation = summarize_refresh(before, after, datetime.now(UTC))
            if native["error"]:
                observation["status"] = "incomplete"
            result.update(observation)
    except KeyboardInterrupt:
        result.update(status="incomplete", error="cancelled")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        result.update(status="incomplete", error="redacted_refresh_failure")
    return result


def live_reload(root, container):
    result = {"probe": "live_reload", "version": VERSION, "status": "incomplete"}
    environment = {
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "broker"),
        "PATH": "/usr/bin:/bin",
    }
    try:
        validate_native(root, environment)
        observed_version(container)
        image = docker(
            ["docker", "inspect", "--format", "{{.Image}}", checked_container(container)]
        )
        image_id = image.stdout.decode("ascii").strip()
        if image.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("container_image")
        result["container_image_id"] = image_id
        before = read_private_auth(root / "broker/auth.json")
        result["initial_injection"] = inject(container, before, "empty-refresh")
        if result["initial_injection"]["status"] != "injected":
            raise ValueError("initial_injection")

        def rotate():
            observation = host_refresh(root)
            if observation["status"] != "rotation_observed":
                return observation, {}
            after = read_private_auth(root / "broker/auth.json")
            if (
                after["tokens"]["account_id"] != before["tokens"]["account_id"]
                or after["tokens"]["access_token"] == before["tokens"]["access_token"]
            ):
                raise ValueError("replacement_injection")
            result["replacement_injection"] = inject(container, after, "empty-refresh")
            if result["replacement_injection"]["status"] != "injected":
                raise ValueError("replacement_injection")
            return observation, after

        exchange = ReloadExchange(before, rotate)
        result["started_at"] = now()
        native, _output = run_native(
            [
                "docker",
                "exec",
                "-i",
                checked_container(container),
                "codex",
                "-c",
                STORE,
                "app-server",
                "--listen",
                "stdio://",
            ],
            dict(os.environ),
            exchange,
            remote=True,
        )
        result.update(native, **exchange.summary(), ended_at=now())
        result["same_process_connection"] = exchange.complete
        if exchange.complete and native["error"] is None:
            result["status"] = "live_reload_observed"
    except KeyboardInterrupt:
        result.update(error="cancelled")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        result.update(error="redacted_reload_failure")
    return result


def existing_session(container):
    result = docker(
        [
            "docker",
            "exec",
            checked_container(container),
            "find",
            "/codex/sessions",
            "-type",
            "f",
            "-name",
            "rollout-*.jsonl",
        ]
    )
    paths = result.stdout.decode("utf-8").splitlines()
    if result.returncode != 0 or len(paths) != 1:
        raise ValueError("single_existing_rollout_required")
    match = re.fullmatch(
        r"/codex/sessions/\d{4}/\d{2}/\d{2}/rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl",
        paths[0],
    )
    if not match:
        raise ValueError("rollout_path")
    return match[1]


def resume_turn(container):
    version = observed_version(container)
    expected = existing_session(container)
    argv = exec_command(container)
    argv[-1:] = ["resume", expected, argv[-1]]
    started = now()
    result = docker(argv)
    ended = now()
    summary = summarize_turn(result.stdout, result.returncode)
    same = False
    if len(result.stdout) <= 4 * 1024 * 1024:
        try:
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            identities = [e.get("thread_id") for e in events if e.get("type") == "thread.started"]
            same = identities == [expected]
        except (ValueError, AttributeError, TypeError):
            pass
    if not same:
        summary.update(status="incomplete", error="native_session_mismatch_or_missing")
    return {
        "container": container,
        "version": version,
        "started_at": started,
        "ended_at": ended,
        "same_native_session": same,
        **summary,
    }


def main():
    parser = argparse.ArgumentParser(description="Opt-in Codex A carrier probe")
    parser.add_argument("--user-run", action="store_true", required=True)
    parser.add_argument("--live-root", type=Path, help="Isolated login root; no default")
    commands = parser.add_subparsers(dest="command", required=True)
    injection = commands.add_parser("inject")
    injection.add_argument("container", choices=CONTAINERS)
    injection.add_argument(
        "variant", choices=("missing-refresh", "empty-refresh", "external-access")
    )
    status = commands.add_parser("status")
    status.add_argument("container", choices=CONTAINERS)
    resume = commands.add_parser("resume")
    resume.add_argument("container", choices=CONTAINERS)
    commands.add_parser("pair")
    commands.add_parser("versions")
    commands.add_parser("expiry")
    commands.add_parser("resume-pair")
    commands.add_parser("host-refresh")
    reload_command = commands.add_parser("live-reload")
    reload_command.add_argument("container", choices=CONTAINERS)
    args = parser.parse_args()
    if args.command in ("host-refresh", "inject", "expiry", "live-reload") and (
        args.live_root is None or not args.live_root.is_absolute() or ".." in args.live_root.parts
    ):
        parser.error("host operations require an absolute --live-root without parent traversal")
    try:
        if args.command == "host-refresh":
            result = host_refresh(args.live_root)
        elif args.command == "live-reload":
            result = live_reload(args.live_root, args.container)
        elif args.command == "inject":
            version = observed_version(args.container)
            result = {
                "version": version,
                **inject(
                    args.container,
                    read_private_auth(args.live_root / "broker/auth.json"),
                    args.variant,
                ),
            }
        elif args.command == "status":
            native = docker(
                ["docker", "exec", args.container, "codex", "-c", STORE, "login", "status"]
            )
            result = {
                "probe": "login_status",
                "container": args.container,
                "status": "loaded" if native.returncode == 0 else "incomplete",
                "exit_code": native.returncode,
            }
            if b"missing field `refresh_token`" in native.stderr:
                result["error"] = "schema_missing_refresh_token"
        elif args.command == "versions":
            result = {"status": "pass", "versions": [observed_version(c) for c in CONTAINERS]}
        elif args.command == "expiry":
            result = access_expiry(
                read_private_auth(args.live_root / "broker/auth.json"), datetime.now(UTC)
            )
        elif args.command == "resume":
            result = {"probe": "native_resume", **resume_turn(args.container)}
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                operation = resume_turn if args.command == "resume-pair" else turn
                observations = list(pool.map(operation, CONTAINERS))
            result = summarize_pair(observations)
            if args.command == "resume-pair":
                result["probe"] = "concurrent_resume"
    except subprocess.TimeoutExpired:
        result = {"status": "incomplete", "error": "timeout_recreate_containers"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        result = {"status": "incomplete", "error": "redacted_probe_failure"}
    print(json.dumps({"recorded_at": now(), **result}, indent=2))
    return (
        0
        if result["status"]
        in ("pass", "loaded", "injected", "observed", "rotation_observed", "live_reload_observed")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
