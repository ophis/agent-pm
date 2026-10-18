import argparse
import base64
import json
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

VERSION = "0.163.0"
CONTAINERS = ("task647-codex-a-1", "task647-codex-a-2")
AUTH = Path("/Users/francis/.agent-pm/work/TASK-647/agent/tmp/codex-live-0.163.0/broker/auth.json")
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


def read_private_auth(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("auth_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open("/", flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("private_directory")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("private_file")
            data = file.read(1024 * 1024 + 1)
    finally:
        os.close(parent)
    if len(data) > 1024 * 1024:
        raise ValueError("auth_size")
    return json.loads(data)


def make_carrier(auth, variant):
    if variant not in ("missing-refresh", "empty-refresh") or auth.get("auth_mode") != "chatgpt":
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
    if variant == "empty-refresh":
        selected["refresh_token"] = ""
    result = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": selected}
    if "last_refresh" in auth:
        if auth["last_refresh"] is not None and not isinstance(auth["last_refresh"], str):
            raise ValueError("refresh_date_type")
        result["last_refresh"] = auth["last_refresh"]
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


def error_item_category(message):
    if not isinstance(message, str):
        return "unknown"
    if message in (
        "Codex couldn't save diagnostic logs to its local database. "
        "Run `codex doctor` for diagnostics.",
        "Codex couldn't save diagnostic logs to its local database. "
        "Use /feedback with logs included before closing Codex, or run `codex doctor` for diagnostics.",
    ):
        return "diagnostic_log_write"
    if (
        message.startswith("Under-development features enabled: ")
        and ". Under-development features are incomplete and may behave unpredictably. "
        "To suppress this warning, set `suppress_unstable_features_warning = true` in "
        in message
        and message.endswith(".")
    ):
        return "under_development_features"
    if message.startswith("Model metadata for `") and message.endswith(
        "` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."
    ):
        return "model_metadata_fallback"
    if message.startswith("Falling back from WebSockets to HTTPS transport. "):
        return "transport_fallback"
    if re.fullmatch(r"in-process app-server event stream lagged; dropped [0-9]+ events", message):
        return "event_stream_lag"
    if (
        message.startswith("model rerouted: ")
        and " -> " in message
        and " (" in message
        and message.endswith(")")
    ):
        return "model_rerouted"
    return "unknown"


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
    parser = argparse.ArgumentParser(description="User-only TASK-647 A carrier probe")
    parser.add_argument("--user-run", action="store_true", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    injection = commands.add_parser("inject")
    injection.add_argument("container", choices=CONTAINERS)
    injection.add_argument("variant", choices=("missing-refresh", "empty-refresh"))
    status = commands.add_parser("status")
    status.add_argument("container", choices=CONTAINERS)
    resume = commands.add_parser("resume")
    resume.add_argument("container", choices=CONTAINERS)
    commands.add_parser("pair")
    commands.add_parser("versions")
    commands.add_parser("expiry")
    commands.add_parser("resume-pair")
    args = parser.parse_args()
    try:
        if args.command == "inject":
            version = observed_version(args.container)
            result = {
                "version": version,
                **inject(args.container, read_private_auth(AUTH), args.variant),
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
            result = access_expiry(read_private_auth(AUTH), datetime.now(UTC))
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
    return 0 if result["status"] in ("pass", "loaded", "injected", "observed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
