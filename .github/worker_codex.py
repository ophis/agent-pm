"""CI-only synthetic checks of the worker's Codex authentication and mount layout."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PYTHON = "/opt/agent-pm/venv/bin/python"
BOOTSTRAP = "/opt/agent-pm/lib/bootstrap.py"
PROBE = """
import json, os, stat, tempfile
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
mounts = {}
for line in Path('/proc/self/mountinfo').read_text().splitlines():
    before, after = line.split(' - ', 1)
    fields = before.split()
    mounts[fields[4]] = (after.split()[0], set(fields[5].split(',')))
for path in (str(home), '/run/agent-pm', '/run/agent-pm/secrets'):
    kind, options = mounts[path]
    assert kind == 'tmpfs'
    assert {'noexec', 'nosuid', 'nodev'} <= options
kind, options = mounts['/opt/agent-pm/runtime']
assert kind == 'tmpfs' and 'noexec' not in options
assert {'nosuid', 'nodev'} <= options
runtime = Path('/opt/agent-pm/runtime')
assert runtime.stat().st_uid == 0 and not runtime.stat().st_mode & 0o022
assert not (runtime / 'codex').exists()
secret = Path('/run/agent-pm/secrets/codex/auth.json')
assert stat.S_IMODE(secret.stat().st_mode) == 0o400
assert secret.stat().st_uid == os.getuid() != 0
assert home.joinpath('auth.json').is_symlink()
document = json.loads(secret.read_bytes())
assert document['tokens']['refresh_token'] == ''
assert document['auth_mode'] == 'chatgptAuthTokens'
identity = home / 'sessions/identity.txt'
if identity.exists():
    assert identity.read_text() == 'synthetic-native-identity'
else:
    identity.write_text('synthetic-native-identity')
fd, temporary = tempfile.mkstemp(dir=home)
with os.fdopen(fd, 'w') as output:
    json.dump(document, output)
os.replace(temporary, home / 'auth.json')
assert not home.joinpath('auth.json').is_symlink()
assert home.joinpath('auth.json').read_bytes()
for path in (home / 'sessions').rglob('*'):
    if path.is_file():
        assert document['tokens']['access_token'].encode() not in path.read_bytes()
"""


def docker(*args, data=None):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"synthetic Codex check: docker {args[0]} failed")
    return result.stdout


def main(image):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("CI") != "true":
        sys.exit("worker_codex.py runs only in GitHub CI")
    if not re.fullmatch(r"agent-pm-worker:[0-9a-f]{64}", image):
        sys.exit("expected a broker-built worker image tag")
    labels = json.loads(docker("image", "inspect", image))[0]["Config"]["Labels"]
    owner = f",mode=0700,uid={labels['agent-pm.agent-uid']},gid={labels['agent-pm.agent-gid']}"
    claims = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": 2000000000,
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "ci-fake",
                        "chatgpt_plan_type": "plus",
                    },
                }
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    access = f"e30.{claims}.synthetic"
    document = json.dumps(
        {
            "auth_mode": "chatgptAuthTokens",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": access,
                "access_token": access,
                "refresh_token": "",
                "account_id": "ci-fake",
            },
            "last_refresh": "2026-10-17T00:00:00Z",
        }
    ).encode()
    with tempfile.TemporaryDirectory(
        dir=os.environ["RUNNER_TEMP"], prefix="codex-state-"
    ) as temporary:
        home = Path(temporary, "codex")
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        sessions.chmod(0o777)
        for _ in range(2):
            name = "agent-pm-codex-ci-" + uuid.uuid4().hex
            try:
                docker(
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    "agent",
                    "--env",
                    f"CODEX_HOME={home}",
                    "--tmpfs",
                    f"{home}:rw,noexec,nosuid,nodev,size=67108864{owner}",
                    "--tmpfs",
                    f"/run/agent-pm:rw,noexec,nosuid,nodev,size=1048576{owner}",
                    "--tmpfs",
                    f"/run/agent-pm/secrets:rw,noexec,nosuid,nodev,size=65536{owner}",
                    "--tmpfs",
                    "/opt/agent-pm/runtime:rw,exec,nosuid,nodev,size=536870912,mode=0755,uid=0,gid=0",
                    "--mount",
                    f"type=bind,source={sessions},target={sessions}",
                    image,
                )
                inspected = json.loads(docker("inspect", name))[0]
                mounts = inspected["Mounts"]
                assert [(m["Source"], m["Destination"]) for m in mounts if m["Type"] == "bind"] == [
                    (str(sessions), str(sessions))
                ]
                assert {str(home), "/run/agent-pm", "/run/agent-pm/secrets"} <= {
                    *inspected["HostConfig"]["Tmpfs"]
                }
                docker("exec", name, PYTHON, "-I", BOOTSTRAP, "codex", str(home), "/workspace")
                stage = """
import runpy
from pathlib import Path
files = list(Path('/opt/agent-pm/clients').glob('**/vendor/*/bin/codex'))
assert len(files) == 1
with files[0].open('rb') as source:
    runpy.run_path('/opt/agent-pm/lib/codex-update.py')['update']('stage', source)
"""
                docker("exec", "--user", "root", name, PYTHON, "-I", "-c", stage)
                assert (
                    docker("exec", name, "/opt/agent-pm/runtime/codex.next", "--version").strip()
                    == b"codex-cli 0.162.1"
                )
                for action in ("publish", "reset"):
                    docker(
                        "exec",
                        "--user",
                        "root",
                        name,
                        PYTHON,
                        "-I",
                        "/opt/agent-pm/lib/codex-update.py",
                        action,
                    )
                    assert (
                        docker("exec", name, "codex", "--version").strip() == b"codex-cli 0.162.1"
                    )
                docker("exec", "-i", name, PYTHON, "-I", BOOTSTRAP, "codex-auth", data=document)
                assert docker("exec", name, "codex", "--version").strip() == b"codex-cli 0.162.1"
                status = docker("exec", name, "codex", "login", "status")
                assert access.encode() not in status
                docker("exec", name, PYTHON, "-I", "-c", PROBE)
            finally:
                docker("rm", "--force", name)
            assert sorted(p.name for p in home.iterdir()) == ["sessions"]
    print("Codex update/rollback, synthetic auth, tmpfs and history persistence: PASS")


if __name__ == "__main__":
    main(sys.argv[1])
