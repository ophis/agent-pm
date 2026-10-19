"""The linear-orchestrator layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

ORCH = "linear_orchestrator"


ORCH_PKG = f"{PKG}.{ORCH}"


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "linear-orchestrator"


ORCH_FILES = f"{ORCH_PROJECT}/{ORCH_SRC}"


COMMAND_DOCS = (f"{ORCH_PROJECT}/skills/",)


ORCH_SESSION_CONTRACT = "linear_orchestrator reaches session only through delegate"


OBJECT_EXPORT_MODULES = (f"{ORCH_FILES}__init__.py",)


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
    (f"{ORCH_FILES}engine/prune.py", "ARCHIVE_ROLES"): "roles whose finished issues are archived",
}


CONFIG_PY = f"{ORCH_SRC}engine/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    *(f"{ORCH_SRC}engine/{name}.py" for name in ("issue", "promote", "prune", "router")),
)


ONCE = (f"{ORCH_PKG}.engine.config.load", f"{ORCH_PKG}.engine.log.use_logs")


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}engine/issues.py"


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
