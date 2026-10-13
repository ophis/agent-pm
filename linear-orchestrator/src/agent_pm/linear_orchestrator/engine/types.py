"""The Linear orchestrator's data types used by two or more of its modules."""

import subprocess
from dataclasses import dataclass


class ConfigError(Exception):
    def __init__(self, msg: str, logs_dir: str | None = None):
        super().__init__(msg)
        self.msg, self.logs_dir = msg, logs_dir

    @classmethod
    def invalid_work_path(cls, v):
        return cls(
            f"linear-orchestrator/config.toml: work_dir must be a printable absolute"
            f" or ~ path: {v!r}"
        )

    @classmethod
    def work_overlaps_repo(cls, real, v):
        return cls(
            f"linear-orchestrator/config.toml: work_dir must neither lie in nor "
            f"contain the repo {real}: {v!r}"
        )

    @classmethod
    def invalid_team_id(cls, cfg_get_team):
        return cls(
            f"linear-orchestrator/config.toml: team must be a Linear team id (UUID): "
            f"{cfg_get_team!r}"
        )

    @classmethod
    def unknown_states(cls, join_extra):
        return cls(f"linear-orchestrator/config.toml: [states] has unknown keys: {join_extra}")

    @classmethod
    def invalid_label_group_id(cls, cfg_get_task_label_group):
        return cls(
            f"linear-orchestrator/config.toml: task_label_group must be a Linear "
            f"label group id (UUID): {cfg_get_task_label_group!r}"
        )

    @classmethod
    def missing_keys(cls, join_missing, local):
        return cls(
            f"linear-orchestrator/config.toml: required, unset: {join_missing}"
            f": set them in {local} (linear-orchestrator/config.toml's `# local:` lines)"
        )

    @classmethod
    def invalid_harness_key(cls, hk):
        return cls(
            f"linear-orchestrator/config.toml: harness_key must be a Keychain service name: {hk!r}"
        )

    @classmethod
    def unknown_keys(cls, join_extra):
        return cls(f"linear-orchestrator/config.toml: unknown keys: {join_extra}")

    @classmethod
    def invalid_projects_table(cls):
        return cls(
            "linear-orchestrator/config.toml: project_repos must be a table of "
            '"<Linear project id>" = "<owner>/<name>"'
        )

    @classmethod
    def invalid_clones_table(cls):
        return cls(
            "linear-orchestrator/config.toml: local_clones must be a table of "
            '"<owner>/<name>" = "<path>"'
        )

    @classmethod
    def invalid_labels_table(cls):
        return cls(
            "linear-orchestrator/config.toml: task_labels must be a table of <task>"
            ' = "<Linear label id>"'
        )

    @classmethod
    def inconsistent_docs_destination(cls, local):
        return cls(
            f"core: document roles must publish to one github.com repo and branch "
            f"(their [output] in {local})"
        )

    @classmethod
    def read_failed(cls, e):
        return cls(f"linear-orchestrator/config.toml: {e}")

    @classmethod
    def core(cls, e):
        return cls(f"core: {e}")

    @classmethod
    def invalid_state_id(cls, k, states_k):
        return cls(
            f"linear-orchestrator/config.toml: states.{k}"
            f" must be a Linear workflow state id (UUID): {states_k!r}"
        )

    @classmethod
    def unknown_role_keys(cls, name, join_extra):
        return cls(
            f"linear-orchestrator/config.toml: [roles.{name}] has unknown keys: {join_extra}"
        )

    @classmethod
    def invalid_max_runs(cls, name, n):
        return cls(
            f"linear-orchestrator/config.toml: [roles.{name}"
            f"] max_runs must be a whole number >= 1: {n!r}"
        )

    @classmethod
    def invalid_project_repo(cls, k, v):
        return cls(
            f"linear-orchestrator/config.toml: project_repos.{k}"
            f" must map a Linear project id (UUID) to <owner>/<name>: {v!r}"
        )

    @classmethod
    def invalid_local_clone(cls, k, v):
        return cls(
            f"linear-orchestrator/config.toml: local_clones.{k}"
            f" must map <owner>/<name> to a printable path starting with / or ~: {v!r}"
        )

    @classmethod
    def duplicate_clone(cls, seen_k_lower, k):
        return cls(
            f"linear-orchestrator/config.toml: local_clones.{seen_k_lower}"
            f" and local_clones.{k} are the same repo"
        )

    @classmethod
    def invalid_task_label(cls, k, v):
        return cls(
            f"linear-orchestrator/config.toml: task_labels.{k}"
            f" must be a Linear label id (UUID): {v!r}"
        )

    @classmethod
    def duplicate_label(cls, seen_v, k, v):
        return cls(
            f"linear-orchestrator/config.toml: task_labels.{seen_v} and task_labels.{k}"
            f" have the same label id {v}"
        )

    @classmethod
    def work_trusted(cls, d, work):
        return cls(f"core: trusted_dirs: {d} is or contains work_dir {work}")

    @classmethod
    def undefined_delegate_role(cls, name):
        return cls(f"linear-orchestrator/config.toml: role {name!r} is not in delegate/config.toml")

    @classmethod
    def undefined_task_role(cls, name):
        return cls(f"linear-orchestrator/config.toml: role {name!r} has no entry in config.TASKS")

    @classmethod
    def shared_harness_key(cls, name, r_key):
        return cls(f"linear-orchestrator/config.toml: [roles.{name}] key {r_key!r} is harness_key")

    @classmethod
    def shared_role_key(cls, name, r_key, owner_r_key):
        return cls(
            f"linear-orchestrator/config.toml: [roles.{name}] key {r_key!r} is also [roles."
            f"{owner_r_key}]'s"
        )

    @classmethod
    def undefined_next_role(cls, name, r_next):
        return cls(
            f"linear-orchestrator/config.toml: next of {name!r} names undefined role {r_next!r}"
        )

    @classmethod
    def next_without_prefix(cls, name, r_next):
        return cls(
            f"linear-orchestrator/config.toml: next of {name!r} is role {r_next!r}"
            f", whose config.TASKS entry has no prefix"
        )

    @classmethod
    def undefined_task(cls, task):
        return cls(
            f"linear-orchestrator/config.toml: task_labels.{task}"
            f" is not a task of a role in linear-orchestrator/config.toml"
        )

    @classmethod
    def local_clones(cls, key, reason):
        return cls(f"linear-orchestrator/config.toml: local_clones.{key}: {reason}")

    @classmethod
    def invalid_role_field(cls, name, k, what, p_get_k):
        return cls(
            f"linear-orchestrator/config.toml: [roles.{name}] {k} must be {what}: {p_get_k!r}"
        )

    @classmethod
    def next_cycle(cls, name):
        return cls(f"linear-orchestrator/config.toml: the next chain from {name!r} has a cycle")


class LinearError(Exception):
    @classmethod
    def api(cls, errors):
        return cls(f"linear api error: {errors}")

    @classmethod
    def invalid_key(cls, service):
        return cls(f"Linear key of {service}: empty, not printable or holds whitespace")

    @classmethod
    def unsuccessful(cls, field):
        return cls(f"{field}: success: false")

    @classmethod
    def foreign_comment(cls, cid):
        return cls(f"comment {cid} is not ours")

    @classmethod
    def human_members_not_found_in_linear(cls, join_missing):
        return cls(
            f"linear-orchestrator/config.toml: human_members not found in Linear: {join_missing}"
        )

    @classmethod
    def team_not_found_in_linear(cls, cfg_team):
        return cls(f"linear-orchestrator/config.toml: team {cfg_team} not found in Linear")

    @classmethod
    def states_not_workflow_states_of_team(cls, t_name, join_bad):
        return cls(
            f"linear-orchestrator/config.toml: [states] not workflow states of team "
            f"{t_name!r}: {join_bad}"
        )

    @classmethod
    def task_label_group_is_not_a_label(cls, group):
        return cls(
            f"linear-orchestrator/config.toml: task_label_group {group} is not a label group"
        )

    @classmethod
    def task_labels_not_labels_of_task_label(cls, group, join_bad):
        return cls(
            f"linear-orchestrator/config.toml: [task_labels] not labels of "
            f"task_label_group {group}: {join_bad}"
        )

    @classmethod
    def roles_account_not_found_in_linear(cls, name, role_account):
        return cls(
            f"linear-orchestrator/config.toml [roles.{name}]: account {role_account!r}"
            f" not found in Linear"
        )

    @classmethod
    def task_label_group_not_found_in_linear(cls, group, e):
        return cls(
            f"linear-orchestrator/config.toml: task_label_group {group} not found in Linear: {e}"
        )


class Stopped(BaseException):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Unavailable(Exception):
    # its text never holds the request, headers or key
    def __init__(self, op, *, status=None, reason=None):
        super().__init__(f"{op}: {reason}" if reason is not None else f"{op}: HTTP {status}")
        self.op, self.status, self.reason = op, status, reason


@dataclass(frozen=True)
class Invalid:
    reason: str


@dataclass(frozen=True)
class Transient:
    reason: str


class IssueError(LookupError):
    @classmethod
    def issue_not_found(cls, ident):
        return cls(f"{ident}: issue not found")

    @classmethod
    def linear_returned_for(cls, str_node_identifier_40, ident):
        return cls(f"Linear returned {str_node_identifier_40!r} for {ident}")


class FileError(ValueError):
    @classmethod
    def over_bytes(cls, max_file):
        return cls(f"over {max_file} bytes")

    @classmethod
    def not_a_regular_file(cls):
        return cls("not a regular file")

    @classmethod
    def not_a_single_link_file(cls):
        return cls("not a single-link file")

    @classmethod
    def not_a_md_file(cls):
        return cls("not a .md file")

    @classmethod
    def not_under_the_workdir(cls):
        return cls("not under the workdir")


class TuiError(ValueError):
    @classmethod
    def tui_session_prefix_want(cls, name, role_issue):
        return cls(f"TUI session prefix {name!r}: want {role_issue}")


class InputError(RuntimeError):
    @classmethod
    def gh_api_contents(cls, path, err_text_res):
        return cls(f"gh api contents {path}: {err_text_res}")

    @classmethod
    def gh_api_contents_unusable_response(cls, d):
        return cls(f"gh api contents {d}: unusable response")


CALL_ERRORS = (
    LinearError,
    OSError,
    ValueError,
    LookupError,
    TypeError,
    RuntimeError,
    subprocess.SubprocessError,
)
SERVICE_ERRORS = (*CALL_ERRORS, Unavailable)
