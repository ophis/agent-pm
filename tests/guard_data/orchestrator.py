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
    (
        f"{ORCH_FILES}engine/prune.py",
        "ARCHIVE_ROLES",
    ): "roles whose Done issues are archived",
}


CONFIG_PY = f"{ORCH_SRC}engine/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    f"{ORCH_SRC}trackers/linear/issue.py",
    *(f"{ORCH_SRC}engine/{name}.py" for name in ("promote", "prune", "router")),
)


ONCE = (
    f"{ORCH_PKG}.engine.config.load",
    f"{ORCH_PKG}.engine.log.use_logs",
)


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}engine/flow/handoff.py"


COMMAND_SRC = (f"{ORCH_PROJECT}/{ORCH_SRC}",)


COMMAND_FILES = (f"{ORCH_PROJECT}/{ORCH_SRC}config.toml",)


PYTHON_CODE_ALLOWED = {
    (
        "orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "165041a70db533bfed166951878a0871c7046a00848c307694b076367a00e9a3",
    ): "Import-time globals and file opens of agent_pm.orchestrator.engine.config (no public re-export) observed in a fresh interpreter.",
    (
        "orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py",
        "cb62981be5b0d5a55a5cfa67837751c7f47c06cdce91be9040a86171ef092469",
    ): "engine.config.load() in a copied-package deployment interpreter; no public loader, and CLI commands that load config go on to call Linear.",
    (
        "orchestrator/tests/orchestrator_tests/integration/linear_run_extension_integration_test.py",
        "b76230139f6d7d55f790ce593752db41b91dc413ad66f2d4dd6be6fbe2cb378b",
    ): "Creating a pipeline run with a config-loaded external Delegate needs engine.config.load() and runs.note; no public loader.",
}


# engine/ imports nothing under trackers/, directly or indirectly, but the board rules' Linear wiring
ENGINE_CONTRACT = "engine imports no tracker"


ENGINE_WIRING = frozenset(
    f"{ORCH_PKG}.engine.{rule} -> {ORCH_PKG}.trackers.linear.{binding}"
    for rule, bindings in (
        ("promote", ("client", "linear_tracker")),
        ("prune", ("client", "linear_tracker")),
        ("router", ("client",)),
    )
    for binding in bindings
)


GRAPHQL = re.compile(r"\b(?:query|mutation)\s*(?:\w+\s*)?(?:\(\s*\$|\{)")


# Linear concepts engine/flow/, the tracker-agnostic core, names none of
FLOW_LINEAR = re.compile(rf"{GRAPHQL.pattern}|linear|graphql|project|email|state id|label id", re.I)
