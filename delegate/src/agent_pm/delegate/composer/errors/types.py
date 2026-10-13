"""Configuration failures with their messages owned by semantic constructors."""


class ConfigError(Exception):
    @classmethod
    def events_detach(cls):
        return cls("--events needs --detach")

    @classmethod
    def detach_events(cls):
        return cls("--detach needs --events")

    @classmethod
    def detach_layout(cls):
        return cls("--split and --split-from need --detach --mode tui")

    @classmethod
    def detach_output(cls):
        return cls("--detach needs --out PATH")

    @classmethod
    def required_paths(cls):
        return cls("--input, --out and --workdir are required")

    @classmethod
    def resume_sid_option(cls):
        return cls("--resume needs --sid")

    @classmethod
    def placeholder(cls, placeholder, where):
        return cls(f"unfilled placeholder {placeholder} in {where}")

    @classmethod
    def heading(cls, path):
        return cls(f"{path}: must start with a '# ' heading")

    @classmethod
    def file_io(cls, path, reason):
        return cls(f"{path}: {reason}")

    @classmethod
    def file_encoding(cls, path):
        return cls(f"{path}: not UTF-8 text")

    @classmethod
    def broken_reference(cls, source, reference):
        return cls(f"{source}: breaks a reference in {reference}")

    @classmethod
    def missing_role_file(cls, role):
        return cls(f"missing file roles/{role}.md")

    @classmethod
    def destination(cls, destination, output_dir, file):
        return cls(f"no destination {destination!r} ({output_dir}/{file})")

    @classmethod
    def undefined_parameter(cls, name, where, source):
        return cls(
            f"{name} is used in {where} but Parameters doesn't define it ({source} is not set)"
        )

    @classmethod
    def obsolete_task_keys(cls, prefix, role, key):
        return cls(
            f"[{prefix}roles.{role}] has {key!r}"
            f": task-level config and default_task are gone; set run keys in [{prefix}roles."
            f"{role}] (a run without a task picks one)"
        )

    @classmethod
    def absolute_cwd(cls, cwd):
        return cls(f"cwd must be an absolute path, got {cwd!r:.80}")

    @classmethod
    def overlay_table(cls, where):
        return cls(f"[{where}] must be a table")

    @classmethod
    def overlay_roles_table(cls, where):
        return cls(f"[{where}.roles] must be a table")

    @classmethod
    def table(cls, where):
        return cls(f"{where} must be a table")

    @classmethod
    def key(cls, key, where):
        return cls(f"unknown key {key!r} in {where}")

    @classmethod
    def task(cls, task, role, tasks):
        return cls(f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")

    @classmethod
    def overlay_role(cls, role, where):
        return cls(f"unknown role {role!r} in [{where}.roles]")

    @classmethod
    def overlay_role_table(cls, where, role):
        return cls(f"[{where}.roles.{role}] must be a table")

    @classmethod
    def overlay(cls, source, cause):
        return cls(f"{source}: {cause}")

    @classmethod
    def tier(cls, tier):
        return cls(f"tier must be an integer 1–4, got {tier!r}")

    @classmethod
    def effort(cls, allowed, effort):
        return cls(f"effort must be one of {', '.join(allowed)}, got {effort!r}")

    @classmethod
    def runtime(cls, runtime):
        return cls(f"runtime must be a non-empty one-line name, got {runtime!r:.80}")

    @classmethod
    def output_type(cls):
        return cls("output.type is not set")

    @classmethod
    def gate(cls):
        return cls("gate must be one line of shell command without backticks")

    @classmethod
    def language(cls):
        return cls("language must be one line of text")

    @classmethod
    def show(cls):
        return cls("show must be one line of shell command")

    @classmethod
    def configured_cwd(cls, cwd):
        return cls(f"cwd must be a printable absolute or ~ path, got {cwd!r:.80}")

    @classmethod
    def no_charter(cls, role):
        return cls(f"roles.{role}: no charter roles/{role}.md in core or team_dirs")

    @classmethod
    def duplicate_file(cls, path, kind, name, original):
        return cls(
            f"{path}: {kind} {name!r} is also defined by {original}; team dir files only add"
        )

    @classmethod
    def front_matter(cls, path, local):
        return cls(
            f"{path}: front matter (---) is not read: team dir files hold text "
            f"only; run keys go in {local}"
        )

    @classmethod
    def file_placeholder(cls, path, placeholder):
        return cls(f"{path}: unfilled placeholder {placeholder}")

    @classmethod
    def team_dirs_override(cls, local, config_name):
        return cls(f"team_dirs: set it in {local}, not {config_name}")

    @classmethod
    def team_dirs_list(cls, value):
        return cls(f"team_dirs: want a list of absolute or ~ paths, got {value!r:.80}")

    @classmethod
    def task_index(cls, path):
        return cls(
            f"{path}: no task index (a ## Tasks section of - `<task>` "
            f"(`<tasks>/<task>.md`): … lines)"
        )

    @classmethod
    def task_default(cls, path):
        return cls(
            f"{path}: ## Tasks opens without its default (unsure → `<task>` (`<tasks>/<task>.md`))"
        )

    @classmethod
    def task_default_unlisted(cls, path, task):
        return cls(f"{path}: the default {task!r} is not a listed task")

    @classmethod
    def core_config(cls, path):
        return cls(f"no core config {path}")

    @classmethod
    def roles_table(cls, path):
        return cls(f"malformed [roles] table in {path}")

    @classmethod
    def unknown_role(cls, role):
        return cls(f"unknown role {role!r}")

    @classmethod
    def missing_file(cls, kind, name):
        return cls(f"missing file {kind}/{name}.md")

    @classmethod
    def missing_method(cls, path, method):
        return cls(f"{path}: names <team>/methods/{method}.md, which core has no file for")

    @classmethod
    def team_filename(cls, path, stem, pattern):
        return cls(f"{path}: name {stem!r}: want {pattern}")

    @classmethod
    def configuration(cls, cause):
        return cls(f"config: {cause}")

    @classmethod
    def team_dir_absolute(cls, path):
        return cls(f"team_dirs: {path!r:.80} is not an absolute or ~ path")

    @classmethod
    def team_dir_missing(cls, path):
        return cls(f"team_dirs: {path} is not a directory")

    @classmethod
    def team_dir_duplicate(cls, path):
        return cls(f"team_dirs: {path} is listed twice")

    @classmethod
    def task_path(cls, path, task, actual):
        return cls(f"{path}: {task!r} has path {actual!r}, not '<tasks>/{task}.md'")

    @classmethod
    def task_ownership(cls, path, task, file, directory):
        return cls(
            f"{path}: lists {task!r}, whose file {file} is not in {directory}"
            f": a role runs only its own tasks"
        )

    @classmethod
    def role_table(cls, role, path):
        return cls(f"malformed [roles.{role}] table in {path}")

    @classmethod
    def team_overlap(cls, path, boundary, directory):
        return cls(
            f"team_dirs: {path} overlaps {boundary} {directory}: the agent run can write there"
        )

    @classmethod
    def start_marks(cls, path, count, mark):
        return cls(f"{path}: has {count} [{mark}:start] lines, not 1")

    @classmethod
    def resume_section(cls, path):
        return cls(f"{path}: has no ## Resume section")

    @classmethod
    def missing_task_file(cls, path, task):
        return cls(f"{path}: lists {task!r} without tasks/{task}.md")

    @classmethod
    def team_dir_temp(cls, path, temporary):
        return cls(f"team_dirs: {path} is in temp dir {temporary}: any process can write there")

    @classmethod
    def team_dir_writable(cls, path):
        return cls(f"team_dirs: {path} is writable by group or others: chmod go-w it")

    @classmethod
    def team_dir_owner(cls, path, owner, current):
        return cls(f"team_dirs: {path} is owned by uid {owner}, not you (uid {current}): chown it")

    @classmethod
    def team_parent_writable(cls, directory, parent):
        return cls(
            f"team_dirs: {directory}'s parent dir {parent}"
            f" is writable by group or others: they could replace the team dir"
        )

    @classmethod
    def team_parent_owner(cls, directory, parent, owner):
        return cls(
            f"team_dirs: {directory}'s parent dir {parent} is owned by uid {owner}"
            f", not you or root: they could replace the team dir"
        )

    @classmethod
    def cwd_missing(cls, cwd):
        return cls(f"cwd {cwd} is not a directory")

    @classmethod
    def cwd_inside_workdir(cls, cwd):
        return cls(
            f"cwd {cwd} is in trusted_dirs and in the workdir: project settings "
            f"must not load from it"
        )

    @classmethod
    def mode(cls, allowed, mode):
        return cls(f"mode must be one of {', '.join(allowed)}, got {mode!r}")

    @classmethod
    def name_mode(cls):
        return cls("name: tui mode only")

    @classmethod
    def events_mode(cls):
        return cls("events: tui mode only")

    @classmethod
    def resume_sid(cls):
        return cls("resume: needs a sid")

    @classmethod
    def layer_configuration(cls, layer, cause):
        return cls(f"{layer} config: {cause}")

    @classmethod
    def policy(cls, runtime, cause):
        return cls(f"policy.{runtime}: {cause}")

    @classmethod
    def untrusted_resume_cwd(cls, cwd):
        return cls(f"cwd {cwd} is no longer in trusted_dirs")

    @classmethod
    def detach_mode(cls, mode):
        return cls(f"--detach: mode must be headless or tui, got {mode!r}")

    @classmethod
    def split_mode(cls):
        return cls("split and split_from: tui mode only")

    @classmethod
    def events_file(cls, path, reason):
        return cls(f"events file {path}: {reason}")
