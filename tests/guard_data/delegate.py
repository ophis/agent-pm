"""The delegate layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

DELEGATE_SRC = f"delegate/src/{PKG}/delegate/"

CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


DELEGATE_INTERFACE = "tests/delegate_interface.json"


DELEGATE_INTERFACE_MODULES = ("agent_pm.delegate", "agent_pm.delegate.extension")


SUBPACKAGES = {
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    "delegate": {"config.py", "extension.py"},
}


OBJECT_EXPORT_MODULES = (
    "delegate/src/agent_pm/delegate/__init__.py",
    "delegate/src/agent_pm/delegate/extension.py",
    "delegate/src/agent_pm/delegate/composer/__init__.py",
    "delegate/src/agent_pm/delegate/event/__init__.py",
)


OBJECT_EXPORTS_ALLOWED = {
    ("agent_pm.delegate", "read_config"): "TASK-389 layer-owned configuration entry point",
    ("agent_pm.delegate", "module_roots"): "TASK-372 layer-owned import-root entry point",
}


OPTION_TUPLES_ALLOWED = {
    (
        f"delegate/src/{PKG}/delegate/event/outcome.py",
        "SAVES_DELIVERABLE",
    ): "the destinations whose deliverable comes back, a subset",
}


COMMAND_SRC = (f"delegate/src/{PKG}/delegate/",)


COMMAND_FILES = ("delegate/src/agent_pm/delegate/config.toml",)


COMMAND_DOCS = ("delegate/skills/", f"delegate/src/{PKG}/delegate/team/")


PYTHON_CODE_ALLOWED = {
    (
        "delegate/tests/integration/child_env_integration_test.py",
        "51acfc7eaec58d79c12d5100cbe097b9584e30bba475b21e0033d969dd628df4",
    ): "Loads the harness test fixtures to inspect child import-root propagation.",
    (
        "delegate/tests/integration/harness_contract_integration_test.py",
        "6975983da86c467a23ac4b286ee499e765b5233015eff2d1b5c77a8700c8a98d",
    ): "Runs the concrete Delegate contract with its fake session in a clean interpreter.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "4cccba13219c3e4414da648a47fda595c685969252709f03e19c8a667cf0821c",
    ): "Exercises concrete harness usage and lifecycle operations across independent processes.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "96d1eb7ab811848f42bf2b53b4d69e39a33493bcbe873a0fcea8dd8963f9047a",
    ): "Exercises concrete harness usage and lifecycle operations across independent processes.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "a1f110b30d41bd8de324eea575cfb67dc998c3843aee85fbeee59c95a396a822",
    ): "Exercises concrete harness usage and lifecycle operations across independent processes.",
    (
        "delegate/tests/integration/harness_owner_integration_test.py",
        "24d2c0b5b0cb3e6311afe9a5f3a28b461749f43957990ff3f4f567acb3c7fc4b",
    ): "Injects the fake session to exercise concrete harness owner lifetime across processes.",
}
