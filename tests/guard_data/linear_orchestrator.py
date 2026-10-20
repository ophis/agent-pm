"""The linear-orchestrator layer's guard data (tests/guard_data/__init__.py)."""

import re

from tests.guards import PKG

ORCH = "linear_orchestrator"


ORCH_PKG = f"{PKG}.{ORCH}"


# config.toml tables keyed by client name: a `# local: <client> = <whole number>` key line in one may name a client;
# the rest of the line, and a real or plain-commented key line, may not
CLIENT_LIMIT_TABLES = ("client_max_runs",)


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "linear-orchestrator"


ORCH_FILES = f"{ORCH_PROJECT}/{ORCH_SRC}"


COMMAND_DOCS = (f"{ORCH_PROJECT}/skills/",)


ORCH_SESSION_CONTRACT = "linear_orchestrator reaches session only through delegate"


OBJECT_EXPORT_MODULES = (f"{ORCH_FILES}__init__.py",)


SUBPACKAGES = {
    ORCH_PROJECT: {"flow"},
}


# FR-25: the router only triggers a run; its Delegate's harness runs it and calls write-back back
ORCH_ABSENT = ("engine/run.py",)


NOQA_ALLOWED = {
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/run_comments.py",
        "BLE001",
        "_bounded.run",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.pending",
    ): "Keep reading the other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.run",
    ): "Keep processing other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "run_prune",
    ): "Prune failures leave promotions intact.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/router.py",
        "BLE001",
        "start_run",
    ): "A recorded launch may fail.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "progress",
    ): "A sink failure must not stop the run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "finish",
    ): "Report the session after failed steps.",
}


OPTION_TUPLES_ALLOWED = {
    (f"{ORCH_FILES}engine/log.py", "ONCE"): "the log fields keying a once-only line",
    (f"{ORCH_FILES}engine/prune.py", "ARCHIVE_ROLES"): "roles whose Done issues are archived",
}


CONFIG_PY = f"{ORCH_SRC}engine/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    *(f"{ORCH_SRC}engine/{name}.py" for name in ("issue", "promote", "prune", "router")),
)


ONCE = (f"{ORCH_PKG}.engine.config.load", f"{ORCH_PKG}.engine.log.use_logs")


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}flow/handoff.py"


COMMAND_SRC = (f"{ORCH_PROJECT}/{ORCH_SRC}",)


COMMAND_FILES = (f"{ORCH_PROJECT}/{ORCH_SRC}config.toml",)


PYTHON_CODE_ALLOWED = {
    (
        "linear-orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "b8828a858303adbef399ec5acdae273d90445023cd4e3b6dbf0a2731d54e87db",
    ): "Import-time globals and file opens of agent_pm.linear_orchestrator.engine.config (no public re-export) observed in a fresh interpreter.",
    (
        "linear-orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "7284022972a2aecaca4b7d03b3d8d4cf183c21363a74a7b5e4403d5bb7e7336a",
    ): "engine.config.load() in a copied-package deployment interpreter; no public loader, and CLI commands that load config go on to call Linear.",
    (
        "linear-orchestrator/tests/orchestrator_tests/integration/linear_run_extension_integration_test.py",
        "c839185c5ed8960ffac563941c624cae8e86f1a8bc4fef19597e9a9f2653ca5c",
    ): "Creating a pipeline run with a config-loaded external Delegate needs engine.config.load() and runs.note; no public loader.",
}


# flow, the engine subpackage, imports no Linear binding module: these, issue and issue_*, and any
# engine module holding GraphQL text
FLOW_CONTRACT = "flow imports no Linear binding module"


FLOW_BINDING = ("client", "ops", "issues", "run_comments", "related")


GRAPHQL = re.compile(r"\b(?:query|mutation)\s*(?:\w+\s*)?(?:\(\s*\$|\{)")


# Linear concepts flow names none of, once the package's own name is dropped
FLOW_LINEAR = re.compile(rf"{GRAPHQL.pattern}|linear|graphql|project|email|state id|label id", re.I)


ORCH_NAMES = re.compile(r"linear[_-]orchestrator")
