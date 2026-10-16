"""Guard data owned by the repository's test infrastructure."""

PYTHON_CODE_ALLOWED = {
    (
        "tests/integration/isolation_integration_test.py",
        "f01519dc60cc2559108fd36dcdc56830ee3ea82b755b849230eecd8f6f8309b9",
    ): "clean_env allocates one per-process TMUX_TMPDIR under a thread race, kept for a sandbox owner or removed at exit; needs fresh interpreters that exit with and without AGENT_PM_TEST_SANDBOX, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "0189d96ba12a767ba469154c71e75bac6f84d71166aff9b2bca21e8b6c21e11f",
    ): "A forked child's clean_env resets to its own TMUX_TMPDIR and removes only that at exit, keeping the parent's fallback directory; needs a fresh unsandboxed interpreter that forks and exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "5b6880ba2d70067ace4d479a90a4103afeb733e97688c6416f180ec7b2382d60",
    ): "clean_env recreates its cached TMUX_TMPDIR after removal, which an unsandboxed process removes at exit; needs fresh interpreters that exit with and without the sandbox, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "625543833e39373ea2f663bd9955446c9eba31d10f3f485f667a53e971774184",
    ): "The atexit fallback cleanup must not hang on utils.tests.sandbox._TMUX_LOCK held by a daemon thread; needs a fresh unsandboxed interpreter that exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "6ce737fd1d32c7edfbaa2a968e5ba5e4dc9e6e3f8685201efdab46b90e445edc",
    ): "Constructs isolation.Gate directly to check the exact isolation.DENIED set and reporting()'s single-file exemption, with its unit/integration argv checks on that gate; tests may not import isolation (ruff TID251) and run_tests.py never exposes reporting().",
    (
        "tests/integration/isolation_integration_test.py",
        "6f3e4f1618b409983a7f99cbb9045fdc3b8e67b4b6dc4400f033658942018b84",
    ): "The atexit fallback cleanup of clean_env's TMUX_TMPDIR stops the tmux servers it owns and skips symlinked ones; needs a fresh unsandboxed interpreter that exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/imports_integration_test.py",
        "b0328cd127a47ff8244ca6bf0c49ce9a53a3d77fd3d6daa81c3979f617e6bc5d",
    ): "Imports each agent_pm module named in argv in a fresh interpreter to check its import-time side effects; the module names are the test's parameters.",
    (
        "tests/integration/module_loading_integration_test.py",
        "8b068c0827660a51d6c9fa6475221d857fa42279c4861a0b7e88609bd5349de8",
    ): "Imports every agent_pm module named in argv in a fresh interpreter to check where each loads from; the module names are the test's parameters.",
}
