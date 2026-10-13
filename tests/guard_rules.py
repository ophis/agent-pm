"""Repository design facts used by placement and ownership checks."""

import re

from agent_pm.utils.config_file import LEVELS

RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*(?:adapters|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/tmux_host.py"
PKG = "agent_pm"
SESSION = "session"
ORCH = "linear_orchestrator"
PROJECTS = {p: p.replace("-", "_") for level in LEVELS for p in level}
LAYERS = tuple(PROJECTS)
ORCH_PKG = f"{PKG}.{ORCH}"
ORCH_SRC = f"src/{PKG}/{ORCH}/"
ORCH_PROJECT = "linear-orchestrator"
LEFTOVERS = {"__pycache__", ".DS_Store"}
TOP = {"__init__.py", "cli.py", "engine"}
SUBPACKAGES = {
    SESSION: {"adapters", "hosts", "modes"},
    "delegate": {"composer", "event", "team"},
    "linear-orchestrator": {"linear"},
}
LOWER = tuple(p for p in PROJECTS if p != ORCH_PROJECT)
TREES = (*PROJECTS, "tests", ".github")
TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))
CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"
LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")
SESSION_SRC = f"session/src/{PKG}/{SESSION}/"
CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"
ADAPTERS_HOME = f"{SESSION_SRC}adapters/registry.py"
STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"
STATUS_LOG_CLOCK = "%H:%M:%S"
COMMAND_SRC = (
    SESSION_SRC,
    f"delegate/src/{PKG}/delegate/",
    f"{ORCH_PROJECT}/{ORCH_SRC}",
)
COMMAND = "agent-pm-"
SESSION_COMMAND = "agent-pm-session"
SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")
LINEAR = re.compile(f"{ORCH_PROJECT}|{ORCH}")
CONFIG_PY = f"{ORCH_SRC}engine/config.py"
TOOL_STRINGS = ("tmux", "adapters")
TOML_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'([^']*)'""")
ENTRIES = tuple(f"{ORCH_SRC}engine/{name}.py" for name in ("run", "promote", "router"))
ONCE = (f"{ORCH_PKG}.engine.config.load", f"{ORCH_PKG}.linear.linear.use_logs")
LOAD_HELPERS = ("_load_config", "_runnable", "_docs")
HANDOFF = re.compile(r"Handoff from|## (?:Source|Instructions|Comments)")
HANDOFF_HOME = f"{ORCH_SRC}linear/issues.py"
COMMAND_FILES = ("delegate/config.toml", f"{ORCH_PROJECT}/config.toml")
COMMAND_DOCS = ("delegate/skills/", "manager/skills/", f"delegate/src/{PKG}/delegate/team/")
PYTHON = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b(?:[ \t]+[^\s`'\"]+){0,3}")
PYTHON_OK = re.compile(
    rf"\S+[ \t]+-P[ \t]+(?:-m[ \t]+{PKG}\.\w"
    r"|(?:<team>|\{\{team\}\})/tools/\w+\.py(?=[ \t]|\Z))"
)
