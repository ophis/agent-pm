"""Configuration failures, formatted from the rejected session settings."""


class ConfigError(Exception):
    @classmethod
    def unknown_adapter_key(cls, key, adapter) -> "ConfigError":
        return cls(f"unknown key {key!r} in {adapter}'s config")

    @classmethod
    def adapter_tables(cls) -> "ConfigError":
        return cls("adapters: want a mapping of [adapters.<name>] tables")

    @classmethod
    def host_tables(cls) -> "ConfigError":
        return cls("hosts: want a mapping of [hosts.<name>] tables")

    @classmethod
    def status_line(cls) -> "ConfigError":
        return cls("status_line: want true or false")

    @classmethod
    def workers_per_column(cls) -> "ConfigError":
        return cls("workers_per_column: want an integer from 1 to 9999")

    @classmethod
    def grid_retile(cls, choices) -> "ConfigError":
        return cls(f"grid_retile: want one of {', '.join(choices)}")

    @classmethod
    def runtime_name(cls, name) -> "ConfigError":
        return cls(f"unsafe runtime name {name!r}: want a lowercase identifier")

    @classmethod
    def missing_adapter(cls, name) -> "ConfigError":
        return cls(f"no [adapters.{name}] table in the configured adapters")

    @classmethod
    def adapter_implementation(cls, name) -> "ConfigError":
        return cls(f'[adapters.{name}].implementation must be "adapters.{name}:<Class>Adapter"')

    @classmethod
    def adapter_load(cls, name, cause) -> "ConfigError":
        return cls(f"adapter {name!r}: {cause}")

    @classmethod
    def adapter_setting(cls, table, key, expected) -> "ConfigError":
        return cls(f"{table}.{key} must be {expected}")

    @classmethod
    def session_uuid(cls, native) -> "ConfigError":
        return cls(f"session ids are UUIDs, got {native!r:.80}")

    @classmethod
    def session_selection(cls, flag) -> "ConfigError":
        return cls(f"{flag}: picks the session; use sid= or resume=")

    @classmethod
    def access_environment(cls) -> "ConfigError":
        return cls("access env must map names ([A-Za-z_][A-Za-z0-9_]*) to strings without NUL")

    @classmethod
    def workdir(cls, value) -> "ConfigError":
        return cls(f"workdir must be a printable absolute path, got {value!r:.80}")

    @classmethod
    def tier(cls, value) -> "ConfigError":
        return cls(f"tier must be an integer 1–4, got {value!r}")

    @classmethod
    def effort(cls, choices, value) -> "ConfigError":
        return cls(f"effort must be one of {', '.join(choices)}, got {value!r}")

    @classmethod
    def cwd(cls, value) -> "ConfigError":
        return cls(f"cwd must be a printable absolute path, got {value!r:.80}")

    @classmethod
    def flags(cls) -> "ConfigError":
        return cls("flags must be a tuple of strings without NUL")

    @classmethod
    def environment(cls) -> "ConfigError":
        return cls("env must map variable names to strings without NUL")

    @classmethod
    def name_type(cls) -> "ConfigError":
        return cls("name must be None or a string")

    @classmethod
    def protected_paths(cls) -> "ConfigError":
        return cls("protect must be a tuple of printable absolute paths")

    @classmethod
    def reply_tags(cls) -> "ConfigError":
        return cls("reply_tags must be None or (progress tag, outcome tag), each [A-Za-z0-9_-]+")

    @classmethod
    def launch_project(cls) -> "ConfigError":
        return cls("launch: project must be a bool or None")

    @classmethod
    def string_tuple(cls, name) -> "ConfigError":
        return cls(f"{name} must be a tuple of strings")

    @classmethod
    def launch_strings(cls, name) -> "ConfigError":
        return cls(f"launch: {name} must be strings without NUL")

    @classmethod
    def protect_mode(cls) -> "ConfigError":
        return cls("protect: tui mode only")

    @classmethod
    def idle_timeout(cls, value) -> "ConfigError":
        return cls(f"idle_timeout must be a positive number of seconds, got {value!r}")

    @classmethod
    def session_name(cls, name) -> "ConfigError":
        return cls(f"name must match [A-Za-z0-9_-]+, got {name!r:.80}")

    @classmethod
    def events_path(cls, events) -> "ConfigError":
        return cls(f"events must be a printable path, got {events!r:.80}")

    @classmethod
    def split_direction(cls, split) -> "ConfigError":
        return cls(f"split must be right or below, got {split!r:.80}")

    @classmethod
    def split_source(cls, name) -> "ConfigError":
        return cls(f"split_from must match [A-Za-z0-9_-]+, got {name!r:.80}")

    @classmethod
    def opener(cls, opener) -> "ConfigError":
        return cls(
            f"opener must be a tmux session name or $ITERM_SESSION_ID's value, got {opener!r:.80}"
        )

    @classmethod
    def tui_option(cls, key) -> "ConfigError":
        return cls(f"{key}: tui mode only")

    @classmethod
    def empty_command(cls, runtime) -> "ConfigError":
        return cls(f"runtime {runtime!r} gave no command")

    @classmethod
    def mode(cls, choices, mode) -> "ConfigError":
        return cls(f"mode must be one of {', '.join(choices)}, got {mode!r}")

    @classmethod
    def resume(cls, value) -> "ConfigError":
        return cls(f"resume must be a session id, got {value!r:.80}")

    @classmethod
    def resume_mismatch(cls, sid, resume) -> "ConfigError":
        return cls(f"sid {sid!r:.80} differs from resume {resume!r:.80}")

    @classmethod
    def runtime_mode(cls, runtime, mode) -> "ConfigError":
        return cls(f"runtime {runtime!r} does not support {mode} mode")

    @classmethod
    def writable_sid(cls) -> "ConfigError":
        return cls("spec.sid is set by writable_dirs(): pass resume=")

    @classmethod
    def match_type(cls, value) -> "ConfigError":
        return cls(f"match must be a regular expression, got {value!r:.80}")

    @classmethod
    def missing_tmux_session(cls, name) -> "ConfigError":
        return cls(f"no tmux session {name}")

    @classmethod
    def argv(cls, value) -> "ConfigError":
        return cls(f"argv must be a non-empty list of str, got {value!r:.80}")

    @classmethod
    def cwd_directory(cls, value) -> "ConfigError":
        return cls(f"cwd must be an existing directory, got {value!r:.80}")

    @classmethod
    def writable_directories(cls, runtime, cause) -> "ConfigError":
        return cls(f"{runtime}'s writable dirs: {cause}")

    @classmethod
    def invalid_match(cls, pattern, cause) -> "ConfigError":
        return cls(f"match {pattern!r:.80}: {cause}")

    @classmethod
    def claude_read_directory(cls, path, forbidden) -> "ConfigError":
        return cls(
            f"claude: read dir {path!r:.80} must be a printable absolute path without {forbidden}"
        )

    @classmethod
    def claude_uuid(cls, sid) -> "ConfigError":
        return cls(f"claude session ids are UUIDs, got {sid!r:.80}")

    @classmethod
    def claude_settings(cls, value) -> "ConfigError":
        return cls(f"--settings {value!r:.80}: want a JSON object with a list of hooks per event")

    @classmethod
    def claude_setting_sources(cls, table) -> "ConfigError":
        return cls(f"--setting-sources is the Driver's: remove it from {table}.flags")

    @classmethod
    def claude_model(cls, tier, table) -> "ConfigError":
        return cls(f"no model for tier {tier} in {table}.tiers")

    @classmethod
    def claude_effort(cls, effort, table) -> "ConfigError":
        return cls(f"no effort for {effort!r} in {table}.efforts")

    @classmethod
    def codex_untrusted_cwd(cls, cwd) -> "ConfigError":
        return cls(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")

    @classmethod
    def codex_untrusted_home(cls, home, workdir) -> "ConfigError":
        return cls(
            f"CODEX_HOME {home} is inside the untrusted workdir {workdir}; move Codex config outside the workdir"
        )

    @classmethod
    def codex_reserved_flag(cls, flag) -> "ConfigError":
        return cls(f"{flag}: the Driver sets Codex's hooks, session and cwd")

    @classmethod
    def codex_mapping(cls, value, table, name) -> "ConfigError":
        return cls(f"no mapping for {value!r} in {table}.{name}")

    @classmethod
    def codex_binding(cls, sid, cwd) -> "ConfigError":
        return cls(f"ambiguous Codex binding for run ID {sid!r} in {cwd}")

    @classmethod
    def codex_recovery(cls, sid) -> "ConfigError":
        return cls(f"missing Codex recovery state for run ID {sid!r}")

    @classmethod
    def codex_native_id(cls, sid) -> "ConfigError":
        return cls(f"missing Codex native ID for run ID {sid!r}")

    @classmethod
    def codex_session_file(cls, native) -> "ConfigError":
        return cls(f"missing Codex session file for native ID {native}")

    @classmethod
    def codex_descendants(cls) -> "ConfigError":
        return cls("duplicate Codex descendant session files")

    @classmethod
    def codex_project_settings(cls, path) -> "ConfigError":
        return cls(
            f"project Codex settings at {path} may load in an untrusted run; use a clean workdir or trusted_dirs"
        )

    @classmethod
    def codex_executable(cls) -> "ConfigError":
        return cls("codex not found on PATH")

    @classmethod
    def codex_hook(cls) -> "ConfigError":
        return cls("Codex tui requires an absolute Driver hook command")

    @classmethod
    def print_prompt_resume(cls) -> "ConfigError":
        return cls("print_prompt has no session to resume")

    @classmethod
    def print_prompt_protocol(cls) -> "ConfigError":
        return cls("print_prompt needs the caller's reply protocol (Spec.reply_tags)")
