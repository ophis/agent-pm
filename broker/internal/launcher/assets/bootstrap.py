import json
import os
import shutil
import stat
import sys
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


def bootstrap_codex(directory, cwd, secrets=Path("/run/agent-pm/secrets")):
    (directory / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        'forced_login_method = "chatgpt"\n'
        "check_for_update_on_startup = false\n"
        f'[projects.{json.dumps(cwd)}]\ntrust_level = "trusted"\n'
    )
    (directory / "auth.json").symlink_to(secrets / "codex/auth.json")


def inject_codex(stream, secrets=Path("/run/agent-pm/secrets")):
    directory = secrets / "codex"
    directory.mkdir(mode=0o700, exist_ok=True)
    temporary = directory / "auth.new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(fd, "wb") as output:
            shutil.copyfileobj(stream, output)
        os.replace(temporary, directory / "auth.json")
    finally:
        temporary.unlink(missing_ok=True)


def main(argv):
    if len(argv) == 3 and argv[0] == "claude":
        bootstrap_claude(TEMPLATES, Path(argv[1]), argv[2])
    elif len(argv) == 3 and argv[0] == "codex":
        bootstrap_codex(Path(argv[1]), argv[2])
    elif argv == ["codex-auth"]:
        inject_codex(sys.stdin.buffer)
    elif len(argv) == 2 and argv[0] == "hook":
        install_hook(STUB, Path(argv[1]))
    else:
        sys.exit("usage: bootstrap.py claude DIR CWD | hook PATH")


if __name__ == "__main__":
    main(sys.argv[1:])
