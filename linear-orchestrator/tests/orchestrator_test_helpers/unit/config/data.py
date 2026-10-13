import shlex
import subprocess

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import GUARD
from orchestrator_test_helpers.unit.board_ids import HEADER, role

BASE = (
    HEADER
    + role("researcher", 'next = "pm"')
    + role("pm", 'next = "engineer"')
    + role("engineer")
    + role("solo")
)

PIPELINE = (
    HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
)

CORE_TOP = 'language = "Chinese"\n'

CORE_TABLES = """[roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""

CORE_LOCAL = CORE_TOP + "\n" + CORE_TABLES

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"

P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"

SLUG = "ophis/agent-pm"

HTTPS = "https://github.com/ophis/agent-pm.git"

STATES_LINE = HEADER[HEADER.index("states = ") : HEADER.index("task_label_group")]

ROOT_Q = shlex.quote(config.ROOT)

LABEL = ("linear-orchestrator/config.toml", "core")

CORE = delegate.CONFIG

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

def missing(keys):
    return (
        f"required, unset: {keys}: set them in ~/.agent-pm/config.local.toml (linear-orchestrator/config.toml's "
        "`# local:` lines)"
    )

def old_key(key):
    return (
        f"core: linear-orchestrator/config.toml: [core.roles.researcher] has {key!r}: task-level config and default_task "
        "are gone; set run keys in [core.roles.researcher] (a run without a task picks one)"
    )

def write(path, text):
    with open(path, "w") as f:
        f.write(text)

ROLES = {
    "researcher": (
        "researcher@agents.test",
        "linear-api-key-researcher",
        ("deep-research", "light-research"),
        1,
        "pm",
    ),
    "pm": ("pm@agents.test", "linear-api-key-pm", ("product-design",), 1, "engineer"),
    "engineer": (
        "engineer@agents.test",
        "linear-api-key-engineer",
        ("build", "light-build"),
        1,
        None,
    ),
}

def fields(roles):
    return {r: (x.account, x.key, x.tasks, x.max_runs, x.next) for r, x in roles.items()}

CORE_TOML = """tier = 2
effort = "high"
runtime = "claude"
output = { type = "local" }

[roles.researcher]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Research/" }

[roles.pm]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }

[roles.engineer]
output = { type = "pull-request" }
"""

DESIGN = 'output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }'

DIRS = {"researcher": "Research/", "pm": "Designs/"}

DEPLOYMENT = {
    "team",
    "states",
    "human_members",
    "harness_key",
    "task_label_group",
    "task_labels",
    "project_repos",
    "local_clones",
    "work_dir",
}

STEPS = (
    "\n## Steps\n\n1. **Report progress:**\n   [agent-pm-progress:start] the topic\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)

def git(*args):
    subprocess.run(
        ["git", *GUARD, *args], check=True, capture_output=True, stdin=subprocess.DEVNULL
    )

IMPORT_ALONE = """import json, os, sys
opened = []
sys.addaudithook(lambda event, args: event == "open" and opened.append(str(args[0])))
from agent_pm.linear_orchestrator.engine import config
import agent_pm.session
home, front = os.environ["HOME"], os.environ["AGENT_PM_PATH"]
os.environ.update(HOME="/later", AGENT_PM_PATH="/later/bin")
print(json.dumps({
    "read": [p for p in opened if p.endswith(".toml") or p.startswith(home)],
    "kept": sorted(k for k, v in vars(config).items() if isinstance(v, str) and (home in v or front in v)),
    "path": config.path(),
}))
"""

ID = "TASK-12"

A, B = "engineer-TASK-12-0b6f2c1e", "dummy-tester-TASK-12-7c1d9e2f"
