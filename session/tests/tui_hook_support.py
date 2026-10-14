import json
import os
import uuid

from session.tests.case import SRC

HOOK = os.path.join(SRC["session"], "agent_pm", "session", "modes", "tui_hook.py")
SOURCE_ROOTS = json.dumps([SRC["session"], SRC["utils"]])
WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}


def install(folder: str, text: str) -> str:
    os.mkdir(folder)
    path = os.path.join(folder, "tmux")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def state(root: str, events) -> dict:
    return {
        "runtime": "fake",
        "sid": str(uuid.uuid4()),
        "workdir": root,
        "cwd": root,
        "project": False,
        "transcript": "",
        "resume": "",
        "events": events,
        "words": dict(WORDS),
        "host_hook": {
            "module": "agent_pm.session.hosts.tmux.hook",
            "state": {
                "executable": os.path.join(root, "bin", "tmux"),
                "target": "w1",
                "generation": uuid.uuid4().hex,
                "environment": {"TMUX_TMPDIR": root},
            },
            "roots": [],
        },
    }


def descriptor(module: str, state: dict | None = None, roots: tuple[str, ...] = ()) -> dict:
    """A session.json callback descriptor, as HookCallback.fields() writes it."""
    return {"module": module, "state": {} if state is None else state, "roots": list(roots)}


def identity(value: dict) -> str:
    return f"{value['sid']}\t{value['host_hook']['state']['generation']}"


def write_state(folder: str, value) -> None:
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    fd = os.open(os.path.join(folder, "session.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
