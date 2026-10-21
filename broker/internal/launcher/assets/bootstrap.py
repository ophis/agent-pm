import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

TEMPLATES = Path("/opt/agent-pm/templates")
STUB = Path("/opt/agent-pm/lib/hook-stub")


def bootstrap_claude(templates, directory, cwd):
    for template in templates.iterdir():
        destination = directory / template.name
        if not os.path.lexists(destination):
            shutil.copyfile(template, destination)
    config = directory / ".claude.json"
    if config.is_symlink():
        sys.exit("bootstrap: .claude.json must not be a symlink")
    data = json.loads(config.read_text())
    data.setdefault("projects", {}).setdefault(cwd, {})["hasTrustDialogAccepted"] = True
    fd = os.open(config, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)


def install_hook(stub, destination, owner=0, top=Path("/")):
    for parent in reversed(destination.parents):
        if parent != top and top not in parent.parents:
            continue
        if not os.path.lexists(parent):
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            sys.exit(
                f"bootstrap: {parent} must be a directory owned by {owner} and not group or other writable"
            )
    if os.path.lexists(destination):
        sys.exit(f"bootstrap: {destination} already exists")
    with destination.open("xb") as stream:
        stream.write(stub.read_bytes())
    destination.chmod(0o755)


def bootstrap_codex(
    directory,
    cwd,
    secrets=Path("/run/agent-pm/secrets"),
    plugins=Path("/opt/agent-pm/codex-plugins"),
):
    native = tomllib.loads((plugins / "config.toml").read_text()).get("plugins", {})
    enabled = "".join(
        f"[plugins.{json.dumps(identifier)}]\nenabled = true\n" for identifier in sorted(native)
    )
    (directory / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        'forced_login_method = "chatgpt"\n'
        "check_for_update_on_startup = false\n"
        f'[projects.{json.dumps(cwd)}]\ntrust_level = "trusted"\n' + enabled
    )
    (directory / "plugins").mkdir(mode=0o700)
    (directory / "plugins/cache").symlink_to(plugins / "plugins/cache", target_is_directory=True)
    (directory / "auth.json").symlink_to(secrets / "codex/auth.json")


def inject_codex(stream, secrets=Path("/run/agent-pm/secrets"), directory=None):
    def reject():
        sys.exit("bootstrap: invalid managed Codex credentials or destination")

    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate credential field")
        return result

    if directory is None:
        value = os.environ.get("CODEX_HOME", "")
        if not value:
            reject()
        directory = Path(value)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        reject()
    try:
        raw = stream.read((1 << 16) + 1)
        data = json.loads(raw, object_pairs_hook=unique)
        tokens = data["tokens"]
        if (
            len(raw) > 1 << 16
            or set(data) != {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
            or data["auth_mode"] != "chatgpt"
            or data["OPENAI_API_KEY"] is not None
            or not isinstance(tokens, dict)
            or set(tokens) != {"id_token", "access_token", "refresh_token", "account_id"}
            or tokens["refresh_token"] != ""
            or any(
                not isinstance(tokens[key], str) or not tokens[key]
                for key in ("id_token", "access_token", "account_id")
            )
            or not isinstance(data["last_refresh"], str)
        ):
            reject()
        destination = directory / "auth.json"
        current_path = destination
        if destination.is_symlink():
            expected = secrets / "codex/auth.json"
            if destination.readlink() != expected or secrets.is_symlink():
                reject()
            if expected.parent.is_symlink():
                reject()
            current_path = expected
        initial = destination.is_symlink() and not os.path.lexists(current_path)
        if os.path.lexists(current_path):
            if not stat.S_ISREG(current_path.lstat().st_mode):
                reject()
            with current_path.open("rb") as current:
                old = json.loads(current.read((1 << 16) + 1))
            if old["tokens"]["account_id"] != tokens["account_id"]:
                reject()
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        reject()
    if initial:
        destination = current_path
        destination.parent.mkdir(mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".auth-", dir=destination.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def stage_runtime(stream, root=Path("/run/agent-pm")):
    files = json.load(stream)
    if not isinstance(files, dict) or len(files) != 1:
        sys.exit("bootstrap: expected one recovery record")
    for name, text in files.items():
        path = Path(name)
        if (
            path.parent.parent != root
            or not path.parent.name.endswith("-store")
            or not re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
            or not isinstance(text, str)
            or len(text.encode()) > 1 << 20
        ):
            sys.exit("bootstrap: invalid recovery projection")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink():
            sys.exit("bootstrap: recovery directory is a link")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(text)


def main(argv):
    if len(argv) == 3 and argv[0] == "claude":
        bootstrap_claude(TEMPLATES, Path(argv[1]), argv[2])
    elif len(argv) == 3 and argv[0] == "codex":
        bootstrap_codex(Path(argv[1]), argv[2])
    elif argv == ["codex-auth"]:
        inject_codex(sys.stdin.buffer)
    elif len(argv) == 2 and argv[0] == "codex-auth":
        if argv[1] != os.environ.get("CODEX_HOME"):
            sys.exit("bootstrap: CODEX_HOME does not match the manifest")
        inject_codex(sys.stdin.buffer, directory=Path(argv[1]))
    elif argv == ["runtime-files"]:
        stage_runtime(sys.stdin)
    elif len(argv) == 2 and argv[0] == "hook":
        install_hook(STUB, Path(argv[1]))
    else:
        sys.exit("usage: bootstrap.py claude DIR CWD | hook PATH")


if __name__ == "__main__":
    main(sys.argv[1:])
