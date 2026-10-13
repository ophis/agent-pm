"""Public utility modules and functions shared by the layers."""

from agent_pm.utils.args import Once
from agent_pm.utils.config_file import LOCAL, ConfigFileError, read_project, unset
from agent_pm.utils.config_file import load as load_config
from agent_pm.utils.files import (
    ConfinedFileError,
    ConfinedReason,
    append_line,
    read_confined,
    read_regular,
    save,
)
from agent_pm.utils.git import (
    GUARD,
    NAME,
    OWNER,
    GitError,
    Invalid,
    Repo,
    check_slug,
    checkout,
    common_dir,
    origin,
    resolve,
)
from agent_pm.utils.git import git as run_git
from agent_pm.utils.markdown import fenced, unfenced
from agent_pm.utils.paths import (
    STATE,
    import_roots,
    module_roots,
    printable_abs,
    real_dirs,
    temp_dirs,
    under,
)
from agent_pm.utils.record import Display, Record, Status, json_records, number
from agent_pm.utils.registry import PATH as CLASS_PATH
from agent_pm.utils.registry import load as load_class
from agent_pm.utils.shell import LONG, SHORT, Runner, Stopped, err_text, redact, resume_command, sh
from agent_pm.utils.text import printable, sid_error

__all__ = [
    "CLASS_PATH",
    "ConfigFileError",
    "ConfinedFileError",
    "ConfinedReason",
    "Display",
    "GUARD",
    "GitError",
    "Invalid",
    "LOCAL",
    "LONG",
    "NAME",
    "OWNER",
    "Once",
    "Record",
    "Repo",
    "Runner",
    "SHORT",
    "STATE",
    "Status",
    "Stopped",
    "append_line",
    "check_slug",
    "checkout",
    "common_dir",
    "err_text",
    "fenced",
    "run_git",
    "import_roots",
    "load_config",
    "json_records",
    "load_class",
    "number",
    "module_roots",
    "origin",
    "printable",
    "printable_abs",
    "read_confined",
    "read_project",
    "read_regular",
    "real_dirs",
    "redact",
    "resolve",
    "resume_command",
    "save",
    "sh",
    "sid_error",
    "temp_dirs",
    "under",
    "unfenced",
    "unset",
]
