"""Production references selected by configuration or standalone file loaders."""

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.codex.loading.types import sibling
from agent_pm.session.adapters.print_prompt import PrintPromptAdapter
from agent_pm.session.hosts.tmux.loading.types import load_file

_dynamic_calls = (
    # Registry resolves session/config.toml's [adapters.claude].implementation through load_class.
    ClaudeAdapter,
    # Registry resolves session/config.toml's [adapters.codex].implementation through load_class.
    CodexAdapter,
    # Registry uses [adapters.print_prompt].implementation in session/config.toml via load_class.
    PrintPromptAdapter,
    # Codex tui_start.py and tui_hook.py obtain this via runpy.run_path(...)["sibling"].
    sibling,
    # Standalone tmux and Codex persistence modules use runpy.run_path(...)["load_file"].
    load_file,
)
