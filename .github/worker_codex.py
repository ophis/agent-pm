"""CI-only synthetic checks of the worker's Codex authentication, plugins and mount layout."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "broker/internal/launcher/assets/Dockerfile"
PYTHON = "/opt/agent-pm/venv/bin/python"
BOOTSTRAP = "/opt/agent-pm/lib/bootstrap.py"
SKILLS_PROBE = """
import json, os, stat, subprocess
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
cache = Path('/opt/agent-pm/codex-plugins/plugins/cache')
root, = (cache / 'claude-autopilot/autopilot').iterdir()
manifest = next(path for path in (root / '.codex-plugin/plugin.json', root / '.claude-plugin/plugin.json') if path.is_file())
skill = root / 'skills/light-build/SKILL.md'
for path in (root, manifest, skill):
    assert path.stat().st_uid == 0
    assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    assert not os.access(path, os.W_OK)
assert home.joinpath('plugins/cache').resolve() == cache
assert not root.is_symlink()
assert json.loads(manifest.read_text())['name'] == 'autopilot'
proc = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def request(identifier, method, params):
    proc.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\\n')
    proc.stdin.flush()
    for line in proc.stdout:
        reply = json.loads(line)
        if reply.get('id') == identifier:
            assert 'error' not in reply
            return reply['result']
    raise AssertionError('app-server closed before its reply')
try:
    request(0, 'initialize', {'clientInfo': {'name': 'agent_pm_ci', 'title': 'Agent PM CI', 'version': '0.0.1'}})
    proc.stdin.write(json.dumps({'method': 'initialized', 'params': {}}) + '\\n')
    proc.stdin.flush()
    found = request(1, 'skills/list', {'cwds': ['/workspace'], 'forceReload': True})
    entry, = found['data']
    assert entry['cwd'] == '/workspace'
    assert not entry['errors']
    loaded, = [s for s in entry['skills'] if s['name'] == 'autopilot:light-build']
    assert loaded['enabled'] is True
    assert loaded['pluginId'] == 'autopilot@claude-autopilot'
    assert Path(loaded['path']).resolve() == skill.resolve()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
"""
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
assert document['auth_mode'] == 'chatgpt'
assert document['tokens']['account_id'] == 'ci-fake'
assert document['tokens']['id_token']
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


RELOAD_PROBE = r"""
import io, json, os, runpy, selectors, signal, subprocess, sys, time
from pathlib import Path

class ReloadExchange:
    def __init__(self, before, after, publish, proactive=None):
        self.before, self.after, self.publish = before, after, publish
        self.proactive = proactive
        self.natural_complete = False
        self.stage = 1
        self.complete = False
        self.notifications = 0

    def request(self, method, params):
        return (json.dumps({'id': self.stage, 'method': method, 'params': params}) + '\n').encode()

    def start(self):
        return self.request('initialize', {
            'clientInfo': {'name': 'agent_pm_ci_reload', 'title': None, 'version': '0.163.0'},
            'capabilities': {'experimentalApi': False, 'explicitGatewayOauth': True},
        })

    def auth_request(self, refresh=False):
        return self.request('getAuthStatus', {'includeToken': True, 'refreshToken': refresh})

    def accept(self, message):
        def reject():
            raise ValueError('synthetic native reload failed')
        if not isinstance(message, dict):
            reject()
        if 'method' in message:
            if 'id' in message or not isinstance(message['method'], str):
                reject()
            self.notifications += 1
            if self.notifications > 1024:
                reject()
            return []
        if ('error' in message or type(message.get('id')) is not int
                or message['id'] != self.stage or self.complete):
            reject()
        result = message.get('result')
        if not isinstance(result, dict):
            reject()
        if self.stage == 1:
            self.stage = 2
            return [b'{"method":"initialized"}\n', self.auth_request()]
        expected = self.before
        if self.stage in (4, 5):
            expected = self.after
        elif self.stage == 6:
            expected = self.proactive['near']
        elif self.stage == 7:
            expected = self.proactive['token']
        if (result.get('authMethod') != 'chatgpt' or result.get('authToken') != expected
                or result.get('requiresOpenaiAuth') is not True):
            reject()
        if self.stage in (4, 6):
            if self.stage == 6:
                self.proactive['publish_final']()
                self.stage = 7
            else:
                self.stage = 5
            return [self.auth_request()]
        if self.stage == 2:
            self.publish()
            self.stage = 3
            return [self.auth_request()]
        if self.stage == 3:
            self.stage = 4
            return [self.auth_request(refresh=True)]
        if self.stage == 5 and self.proactive is not None:
            self.proactive['publish_near']()
            self.stage = 6
            return [self.auth_request(refresh=True)]
        self.natural_complete = self.stage == 7
        self.complete = True
        return []

    def summary(self):
        return {'guarded_reload_completed': self.complete, 'natural_auth_reload_completed': self.natural_complete,
                'notification_count': self.notifications}

class BoundedLines:
    def __init__(self):
        self.pending = bytearray()
        self.total = 0

    def feed(self, data):
        self.total += len(data)
        if self.total > 1024 * 1024:
            raise ValueError('synthetic native output limit')
        self.pending.extend(data)
        messages = []
        while b'\n' in self.pending:
            line, _, remaining = self.pending.partition(b'\n')
            self.pending = remaining
            if len(line) > 65536:
                raise ValueError('synthetic native line limit')
            messages.append(json.loads(line))
        if len(self.pending) > 65536:
            raise ValueError('synthetic native line limit')
        return messages

    def finish(self):
        if self.pending:
            raise ValueError('synthetic native incomplete output')

def stop_native(process):
    try:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.25)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

def run_reload(home, initial, replacement, near, adopted):
    documents = (initial, replacement, near, adopted)
    if (any(document['auth_mode'] != 'chatgpt' or document['tokens']['refresh_token'] != ''
            or document['tokens']['account_id'] != initial['tokens']['account_id'] for document in documents)
            or len({document['tokens']['access_token'] for document in documents}) != len(documents)):
        raise ValueError('synthetic managed same-account values required')
    bootstrap = runpy.run_path('/opt/agent-pm/lib/bootstrap.py')
    def publish(document):
        bootstrap['inject_codex'](io.BytesIO(json.dumps(document).encode()), directory=home)
    exchange = ReloadExchange(
        initial['tokens']['access_token'], replacement['tokens']['access_token'], lambda: publish(replacement),
        proactive={'token': adopted['tokens']['access_token'], 'near': near['tokens']['access_token'],
                   'publish_near': lambda: publish(near),
                   'publish_final': lambda: publish(adopted)},
    )
    process = subprocess.Popen(
        ['codex', '-c', 'cli_auth_credentials_store="file"', 'app-server', '--listen', 'stdio://'],
        cwd=home,
        env={'HOME': str(home), 'CODEX_HOME': str(home),
             'PATH': '/opt/agent-pm/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        process.stdin.write(exchange.start())
        process.stdin.flush()
        decoder = BoundedLines()
        stderr_size = 0
        deadline = time.monotonic() + 120
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            while not exchange.complete:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('synthetic native timeout')
                for key, _ in selector.select(timeout=min(0.2, remaining)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.data == 'stdout':
                            decoder.finish()
                            if not exchange.complete:
                                raise ValueError('synthetic native closed before adoption')
                        continue
                    if key.data == 'stderr':
                        stderr_size += len(data)
                        if stderr_size > 1024 * 1024:
                            raise ValueError('synthetic native stderr limit')
                        continue
                    for message in decoder.feed(data):
                        for request in exchange.accept(message):
                            process.stdin.write(request)
                            process.stdin.flush()
        return exchange.summary()
    finally:
        stop_native(process)

if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(65537))
        result = run_reload(Path(os.environ['CODEX_HOME']), payload['initial'], payload['replacement'],
                            payload['near'], payload['adopted'])
        assert result['guarded_reload_completed'] is True
        assert result['natural_auth_reload_completed'] is True
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, subprocess.SubprocessError):
        sys.exit('synthetic native guarded reload failed')
    print('Synthetic same-process guarded reload: PASS')
"""


def synthetic_credentials(lifetime=540):
    now = int(time.time())
    refreshed = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
    documents = []
    for marker in ("initial", "replacement"):
        claims = {
            "exp": now + lifetime,
            "sub": "ci-fake-user",
            "email": "ci-probe@example.invalid",
            "jti": marker + "-" + uuid.uuid4().hex,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "ci-fake",
                "chatgpt_user_id": "ci-fake-user",
                "chatgpt_plan_type": "plus",
            },
        }
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        access = f"e30.{encoded}.synthetic"
        documents.append(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": access,
                        "access_token": access,
                        "refresh_token": "",
                        "account_id": "ci-fake",
                    },
                    "last_refresh": refreshed,
                }
            ).encode()
        )
    return tuple(documents)


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
    [pinned] = re.findall(r"@openai/codex@(\S+)", DOCKERFILE.read_text(encoding="utf-8"))
    version = f"codex-cli {pinned}".encode()
    labels = json.loads(docker("image", "inspect", image))[0]["Config"]["Labels"]
    owner = f",mode=0700,uid={labels['agent-pm.agent-uid']},gid={labels['agent-pm.agent-gid']}"
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
                    f"{home}:rw,noexec,nosuid,nodev,size=1073741824{owner}",
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
                    == version
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
                    assert docker("exec", name, "codex", "--version").strip() == version
                docker("exec", name, PYTHON, "-I", "-c", SKILLS_PROBE)
                document, replacement = synthetic_credentials()
                near, _ = synthetic_credentials(lifetime=120)
                _, adopted = synthetic_credentials()
                access = json.loads(document)["tokens"]["access_token"]
                docker(
                    "exec",
                    "-i",
                    name,
                    PYTHON,
                    "-I",
                    BOOTSTRAP,
                    "codex-auth",
                    str(home),
                    data=document,
                )
                assert docker("exec", name, "codex", "--version").strip() == version
                status = docker("exec", name, "codex", "login", "status")
                assert access.encode() not in status
                docker("exec", name, PYTHON, "-I", "-c", PROBE)
                payload = json.dumps(
                    {
                        "initial": json.loads(document),
                        "replacement": json.loads(replacement),
                        "near": json.loads(near),
                        "adopted": json.loads(adopted),
                    }
                ).encode()
                checked = docker("exec", "-i", name, PYTHON, "-I", "-c", RELOAD_PROBE, data=payload)
                assert checked.strip() == b"Synthetic same-process guarded reload: PASS"
            finally:
                docker("rm", "--force", name)
            assert sorted(p.name for p in home.iterdir()) == ["sessions"]
    print(
        "Codex updates, native skills, synthetic guarded reload, tmpfs and history persistence: PASS"
    )


if __name__ == "__main__":
    main(sys.argv[1])
