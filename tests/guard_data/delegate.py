"""The delegate layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

DELEGATE_SRC = f"delegate/src/{PKG}/delegate/"

CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


DELEGATE_INTERFACE = "tests/delegate_interface.json"


DELEGATE_INTERFACE_MODULES = ("agent_pm.delegate",)


SUBPACKAGES = {
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    "delegate": {"config.py"},
}


OBJECT_EXPORT_MODULES = (
    "delegate/src/agent_pm/delegate/__init__.py",
    "delegate/src/agent_pm/delegate/composer/__init__.py",
    "delegate/src/agent_pm/delegate/event/__init__.py",
)


OBJECT_EXPORTS_ALLOWED = {}


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
        "delegate/tests/integration/harness_host_integration_test.py",
        "2da94b78c066e8e0f5ae058c730ee3a6f208b3c4bde29b6666cf913d88749860",
    ): "Loads the unexported delegate config reader in a fresh interpreter to configure Session for the real harness Host test.",
    (
        "delegate/tests/integration/child_env_integration_test.py",
        "51acfc7eaec58d79c12d5100cbe097b9584e30bba475b21e0033d969dd628df4",
    ): "Harness PYTHONPATH and composer access env under a source vs installed layout need a fresh interpreter with HarnessCase's RecordingHost, injectable only in-process.",
    (
        "delegate/tests/integration/composer/child.py",
        "1721efcb4ec2389d5c2a1c62958402376e88ba56fe4415bd678555bff34109f4",
    ): "Composer access/render/check_team_dirs (agent_pm.delegate.composer, not re-exported) in a fresh -P -S interpreter on the src roots, asserting its modules load from this tree and no team tools/ module is imported.",
    (
        "delegate/tests/integration/harness_contract_integration_test.py",
        "f0d067b985e08af56357fe84b0b297618beeb741597f4c616f6a7ad9dc194b1a",
    ): "The contract's class-method test calls HarnessDelegate.usage without session_cls, which configures the process-global Session from config; tests may not patch HostedSession._setup, so only a fresh interpreter keeps that out of the test process.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "96dc3a20fb0bec1d0b6dc040878360d93011b9ca57335bef35fb982a55505cc1",
    ): "HarnessDelegate.usage with its default session_cls configures the process-global Session and loads UsageClient through it; no public fresh session class exists and tests may not configure or patch the shared Session in-process.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "83e2486cff9dd4dc40383f5fddf383521e8e35a63679cb503ad28a49a8947aed",
    ): "Two-process detach race needs PersistentSession's disk-backed fake, injectable only as a session_cls class object.",
    (
        "delegate/tests/integration/harness_owner_integration_test.py",
        "844f553ea872f665a624970b7315cc589051acd75796dd8f2a437a0c47327385",
    ): "Foreground owner process needs the scripted process-free HarnessSession (delegate.tests), injectable only as a session_cls class object.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "delegate/tests/fakes/callback_recorder.py",
        "9eab95e939ba98ce47fe6013b159a0526b7a4d2e044077d2b53755ba304ec33f",
    ): "Existing callback recorder fixture delays a child to observe callback ordering.",
    (
        "delegate/tests/fakes/usage.py",
        "f4679a533131770a0d73388e96fb8e954fb5cf1bb2520271c87ba4f45cc0d916",
    ): "Existing usage fake emits a deterministic quota response.",
    (
        "delegate/tests/integration/bin_integration_test.py",
        "c710e2832b40273e5a68ddb9026256785f0d2f47e44d3f285c6132d45114a427",
    ): "Existing executable-selection fixture checks the system interpreter meets Python 3.11.",
    (
        "delegate/tests/integration/child_env_integration_test.py",
        "51acfc7eaec58d79c12d5100cbe097b9584e30bba475b21e0033d969dd628df4",
    ): "Existing environment fixture captures client launch environments from a separate caller process.",
    (
        "delegate/tests/integration/harness_contract_integration_test.py",
        "f0d067b985e08af56357fe84b0b297618beeb741597f4c616f6a7ad9dc194b1a",
    ): "Existing process fixture checks class methods answer for its roles and refuse an unknown one.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "96dc3a20fb0bec1d0b6dc040878360d93011b9ca57335bef35fb982a55505cc1",
    ): "Existing process fixture checks role client usage preserves sentinel identity and none without starting.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "83e2486cff9dd4dc40383f5fddf383521e8e35a63679cb503ad28a49a8947aed",
    ): "Existing process fixture checks two processes launch one harness.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "ed0a2204e27345a82a6f28a5294fbf74196698727334665adb62ce7952e11a2c",
    ): "Existing process fixture checks two processes resume once and return the same record.",
    (
        "delegate/tests/integration/harness_owner_integration_test.py",
        "844f553ea872f665a624970b7315cc589051acd75796dd8f2a437a0c47327385",
    ): "Existing harness ownership fixture starts an independent owner process for recovery checks.",
}
