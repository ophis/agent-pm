import json
import os
import pathlib
import shutil
import stat
import sys

mode, target, *args = sys.argv[1:]
if mode == "claude":
    directory = pathlib.Path(target)
    for template in pathlib.Path("/opt/agent-pm/templates").iterdir():
        destination = directory / template.name
        if not os.path.lexists(destination):
            shutil.copyfile(template, destination)
    config = directory / ".claude.json"
    if config.is_symlink():
        sys.exit("bootstrap config must not be a symlink")
    data = json.loads(config.read_text())
    data.setdefault("projects", {}).setdefault(args[0], {})["hasTrustDialogAccepted"] = True
    config.write_text(json.dumps(data))
elif mode == "hook":
    destination = pathlib.Path(target)
    for parent in reversed(destination.parents):
        if not os.path.lexists(parent):
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            sys.exit("hook parents must be root-owned and not writable by agent")
    if os.path.lexists(destination):
        sys.exit("hook path already exists")
    with destination.open("xb") as stream:
        stream.write(pathlib.Path("/opt/agent-pm/lib/hook-stub").read_bytes())
    destination.chmod(0o755)
else:
    sys.exit("unknown bootstrap mode")
