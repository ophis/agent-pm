"""The orchestrator layer's guard data (tests/guard_data/__init__.py)."""

import re

from tests.guards import PKG

ORCH = "orchestrator"


ORCH_PKG = f"{PKG}.{ORCH}"


# config.toml tables keyed by client name: a `# local: <client> = <whole number>` key line in one may name a client;
# the rest of the line, and a real or plain-commented key line, may not
CLIENT_LIMIT_TABLES = ("client_max_runs",)


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "orchestrator"


ORCH_FILES = f"{ORCH_PROJECT}/{ORCH_SRC}"


COMMAND_DOCS = (f"{ORCH_PROJECT}/skills/",)


ORCH_SESSION_CONTRACT = "orchestrator reaches session only through delegate"


OBJECT_EXPORT_MODULES = (f"{ORCH_FILES}__init__.py",)


SUBPACKAGES = {
    ORCH_PROJECT: {"trackers"},
}


# FR-25: the router only triggers a run; its Delegate's harness runs it and calls write-back back
ORCH_ABSENT = ("trackers/linear/run.py",)


NOQA_ALLOWED = {
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/run_comments.py",
        "BLE001",
        "_bounded.run",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/promote.py",
        "BLE001",
        "Promoter.pending",
    ): "Keep reading the other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/promote.py",
        "BLE001",
        "Promoter.run",
    ): "Keep processing other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/promote.py",
        "BLE001",
        "run_prune",
    ): "Prune failures leave promotions intact.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/router.py",
        "BLE001",
        "start_run",
    ): "A recorded launch may fail.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/writeback.py",
        "BLE001",
        "progress",
    ): "A sink failure must not stop the run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}trackers/linear/writeback.py",
        "BLE001",
        "finish",
    ): "Report the session after failed steps.",
}


OPTION_TUPLES_ALLOWED = {
    (f"{ORCH_FILES}trackers/linear/log.py", "ONCE"): "the log fields keying a once-only line",
    (
        f"{ORCH_FILES}trackers/linear/prune.py",
        "ARCHIVE_ROLES",
    ): "roles whose Done issues are archived",
}


CONFIG_PY = f"{ORCH_SRC}trackers/linear/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    *(f"{ORCH_SRC}trackers/linear/{name}.py" for name in ("issue", "promote", "prune", "router")),
)


ONCE = (
    f"{ORCH_PKG}.trackers.linear.config.load",
    f"{ORCH_PKG}.trackers.linear.log.use_logs",
)


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}engine/handoff.py"


COMMAND_SRC = (f"{ORCH_PROJECT}/{ORCH_SRC}",)


COMMAND_FILES = (f"{ORCH_PROJECT}/{ORCH_SRC}config.toml",)


PYTHON_CODE_ALLOWED = {
    (
        "orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "f280501cb853c1b3aba91eab98fb1b7e5494e52e76d374cfb4e5e1eb42b54aa5",
    ): "Import-time globals and file opens of agent_pm.orchestrator.trackers.linear.config (no public re-export) observed in a fresh interpreter.",
    (
        "orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "a2d134686f81af24ad735e68cf9d621d9155acf872080a3b5980f242e6472d47",
    ): "trackers.linear.config.load() in a copied-package deployment interpreter; no public loader, and CLI commands that load config go on to call Linear.",
    (
        "orchestrator/tests/orchestrator_tests/integration/linear_run_extension_integration_test.py",
        "4b5296e4b98e450496c5e9c0b2719a54289d75712aa80aa69f457228935b0d69",
    ): "Creating a pipeline run with a config-loaded external Delegate needs trackers.linear.config.load() and runs.note; no public loader.",
}


# engine/ imports nothing under trackers/, directly or indirectly, with no exception
ENGINE_CONTRACT = "engine imports no tracker"


GRAPHQL = re.compile(r"\b(?:query|mutation)\s*(?:\w+\s*)?(?:\(\s*\$|\{)")


# Linear concepts engine/ names none of
ENGINE_LINEAR = re.compile(
    rf"{GRAPHQL.pattern}|linear|graphql|project|email|state id|label id", re.I
)
