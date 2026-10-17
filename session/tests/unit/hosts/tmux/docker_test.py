"""hosts/tmux/docker.py: DockerHost's config, start order and rollback, container commands, the Host
record, listing, close and show, over a fake docker CLI, a fake broker and a recorded host tmux.
"""

import fcntl
import io
import json
import os
import signal
import stat
import tomllib
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from agent_pm.session.engine.types import (
    ClientHome,
    ConfigError,
    KeyPress,
    Launch,
    Liveness,
    Paste,
    SessionError,
    StatusState,
)
from agent_pm.session.hosts.base import Host
from agent_pm.session.hosts.docker.broker import RELAY, BrokerError, client_keys
from agent_pm.session.hosts.docker.daemon import CLEARED, RUN_TIMEOUT, Overflow
from agent_pm.session.hosts.docker.relay import SOCKET
from agent_pm.session.hosts.docker.types import Started
from agent_pm.session.hosts.tmux.docker import DockerHost, DockerTerminal
from agent_pm.session.hosts.tmux.transport import Tmux
from agent_pm.utils import Record, parse_time

from session.tests.case import REPO, Case
from utils.tests.process_fixtures import TimeoutExpired

DOCKER = "/usr/local/bin/docker"
GEN = "0123456789abcdef0123456789abcdef"
CONTAINER = f"agent-pm-w-{GEN}"
PROCESS = "4242 $3 %7 4343"
IMAGE = "agent-pm-worker:" + "a" * 64
DIGEST = "sha256:" + "c" * 64
HOME = ClientHome("agent_dir", "agent", "AGENT_HOME", "agent-token")
RECORD = Record(
    1,
    "agent",
    "w",
    (),
    "s1",
    None,
    ("agent-pm-session",),
    ("agent-pm-session", "resume", "w"),
    None,
    None,
)


def pane(dead="0  "):
    """display-message's answer for the pane PROCESS: its id, its DEAD fields, or both."""

    def reply(cmd):
        fmt = cmd[-1]
        if "\t" in fmt:
            return done(0, f"{PROCESS}\t{dead}\n")
        return done(0, f"{dead}\n" if fmt.startswith("#{pane_dead}") else f"{PROCESS}\n")

    return reply


def done(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeDocker:
    """The docker CLI: `ps` prints `ps`; an exec answers from `replies`, {command prefix: (rc, out,
    err), an exception or a callable of the command}, the first matching prefix; else success.
    """

    def __init__(self, ep, order):
        self.ep, self.order, self.ps, self.replies, self.inputs = ep, order, [], {}, []

    def __call__(self, argv, env, *, input=None, timeout):
        assert argv[:3] == [DOCKER, "--host", self.ep], argv
        assert not set(CLEARED) & env.keys() and timeout <= RUN_TIMEOUT
        self.order.append(argv[3:])
        self.inputs.append(input)
        if argv[3] == "ps":
            return done(0, "".join(f"{line}\n" for line in self.ps))
        cmd = argv[6:]
        for prefix, reply in self.replies.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if isinstance(reply, BaseException):
                    raise reply
                return reply(cmd) if callable(reply) else done(*reply)
        return done()

    def execs(self):
        return [call[3:] for call in self.order if isinstance(call, list) and call[0] == "exec"]


class FakeBroker:
    """`failing`: {command: the error it raises}."""

    def __init__(self, order, started):
        self.order, self.started, self.error, self.closed = order, started, None, []
        self.failing = {}

    def version(self):
        self.order.append("version")
        if "version" in self.failing:
            raise self.failing["version"]

    def build(self, package):
        self.order.append(("build", package))
        if "build" in self.failing:
            raise self.failing["build"]
        return IMAGE, DIGEST

    def start(self, manifest, *, clients):
        self.order.append(("start", manifest, clients))
        if self.error:
            raise self.error
        return self.started

    def close(self, container):
        self.order.append(("close", container))
        self.closed.append(container)
        return True


class Base(Case):
    def setUp(self):
        self.account = os.path.realpath(self.home)
        patcher = mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=self.account))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ep = f"unix://{self.account}/.docker/run/docker.sock"
        os.makedirs(os.path.dirname(self.ep.removeprefix("unix://")))
        open(self.ep.removeprefix("unix://"), "w").close()
        self.cwd = os.path.join(self.account, "src", "repo")
        os.makedirs(self.cwd)
        self.order, self.host_calls = [], []
        self.docker = FakeDocker(self.ep, self.order)
        started = Started(CONTAINER, self.ep, "sha256:" + "b" * 64, {}, {"status": "updated"})
        self.broker = FakeBroker(self.order, started)
        self.host_replies = {}
        self.host = DockerHost(
            "docker",
            {"docker": DOCKER, "mount_roots": [os.path.join(self.account, "src")]},
            tmux=Tmux(self.host_tmux),
            run=self.docker,
            broker=self.broker,
        )
        self.state = os.path.join(self.home, ".agent-pm", "docker-state", "w")

    def host_tmux(self, argv, **kw):
        self.host_calls.append(argv[1:])
        for prefix, reply in self.host_replies.items():
            if tuple(argv[1 : 1 + len(prefix)]) == prefix:
                return done(*reply)
        return done(1, "", "no server running on /tmp/tmux-501/default")

    def record(self, **changes):
        found = {
            "generation": GEN,
            "sid": "s1",
            "client": "agent",
            "display": "lead/below",
            "record": None,
            "hidden": False,
            "process": PROCESS,
            **changes,
        }
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "host.json"), "w") as f:
            json.dump(found, f)

    def host_record(self):
        with open(os.path.join(self.state, "host.json")) as f:
            return json.load(f)

    def listed(self, *names):
        self.docker.ps = [f"agent-pm-{name}-{GEN}\t{name}\t{GEN}" for name in names]

    def terminal(self):
        self.listed("w")
        self.record()
        return self.host.attach("w")


class ConfigTest(Base):
    def test_the_packaged_table_parses_and_tmux_stays_the_default(self):
        with open(os.path.join(REPO, "session/src/agent_pm/session/config.toml"), "rb") as f:
            table = tomllib.load(f)["hosts"]
        self.assertEqual(
            table["docker"],
            {
                "implementation": "agent_pm.session.hosts.tmux.docker:DockerHost",
                "docker": "/usr/local/bin/docker",
                "mount_roots": [],
            },
        )
        self.assertEqual(Host.parse_hosts(table).default, "tmux")
        settings = {k: v for k, v in table["docker"].items() if k != "implementation"}
        self.assertEqual(DockerHost("docker", settings).roots, [])

    def test_bad_settings_name_their_key(self):
        for settings, key in (
            ({"docker": DOCKER, "other": 1}, "'other'"),
            ({"docker": "docker"}, "docker"),
            ({"docker": DOCKER, "mount_roots": "/src"}, "mount_roots"),
            ({"docker": DOCKER, "mount_roots": ["/src", 1]}, "mount_roots"),
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(ConfigError, key):
                DockerHost("docker", settings)


class ClientEnvTest(Base):
    def test_a_client_without_home_is_refused(self):
        for client, named in (("other", "client other"), ("", r"client \(unnamed\)")):
            with (
                self.subTest(client=client),
                self.assertRaisesRegex(
                    ConfigError, rf"{named}: not supported on the Docker Host yet"
                ),
            ):
                self.host._client_env("w", client, None, resume=False)

    def test_resume_gets_the_same_env_and_touches_no_file(self):
        self.assertEqual(
            self.host._client_env("w", "agent", HOME, resume=True),
            {"AGENT_HOME": os.path.join(self.home, ".agent-pm", "docker-state", "w", "agent")},
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(self.host._inherit_env)

    def test_resume_argv_names_the_session(self):
        self.assertEqual(self.host._resume_argv("w"), ("agent-pm-session", "resume", "w"))
        self.assertIsNone(Host._resume_argv(self.host, "w"))

    def test_client_env_points_at_the_home(self):
        self.assertEqual(
            self.host._client_env("w", "agent", HOME, resume=False),
            {"AGENT_HOME": os.path.join(self.home, ".agent-pm", "docker-state", "w", "agent")},
        )


class StartTest(Base):
    METADATA = {
        "sid": "s1",
        "client": "agent",
        "generation": GEN,
        "home_field": "agent_dir",
        "home_dir": "agent",
        "resume": "0",
        "credential": "agent-token",
    }

    def setUp(self):
        super().setUp()
        self.home_dir = os.path.join(self.state, "agent")
        self.docker.replies = {
            ("tmux", "new-session"): (0, PROCESS + "\n", ""),
            ("tmux", "display-message"): pane(),
        }
        self.supervisor = mock.Mock()
        popen = mock.patch("subprocess.Popen", side_effect=self.popen)
        self.popen_mock = popen.start()
        self.addCleanup(popen.stop)
        self.launch = Launch(
            ("agent", "--flag"),
            cwd=self.cwd,
            env={"PATH": "/host/bin", "AGENT_HOME": self.home_dir, "K": "v"},
        )

    def popen(self, argv, **kw):
        self.order.append("supervisor")
        return self.supervisor

    def start(self, **metadata):
        return self.host.terminal(
            "w",
            self.launch,
            display="lead/below",
            events=os.path.join(self.home, "events"),
            record=RECORD,
            metadata={k: v for k, v in {**self.METADATA, **metadata}.items() if v is not None},
        )

    def history(self):
        """A native transcript in the home, which the launch names."""
        transcript = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w") as f:
            f.write("{}\n")
        self.launch = replace(self.launch, transcript=transcript)
        return transcript

    def refused_untouched(self, error, pattern):
        """start(resume="1") fails with `pattern`, before any broker call, file or supervisor."""
        self.order.clear()
        with self.assertRaisesRegex(error, pattern):
            self.start(resume="1")
        self.assertNotIn("version", self.order)
        self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.json")))
        self.popen_mock.assert_not_called()

    def test_start_order(self):
        os.makedirs(self.home_dir)
        with open(os.path.join(self.home_dir, "old"), "w"):
            pass
        process = self.start()
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertEqual(
            [step if isinstance(step, str | tuple) else step[:5] for step in self.order],
            [
                ["ps", "-a", "--filter", "label=agent-pm.session", "--format"],
                "version",
                ("build", REPO),
                ("start", manifest, client_keys(REPO)),
                "supervisor",
                ["exec", "-i", CONTAINER, "test", "-S"],
                ["exec", "-i", CONTAINER, "tmux", "new-session"],
                ["exec", "-i", CONTAINER, "tmux", "display-message"],
            ],
        )
        self.assertEqual(os.listdir(self.home_dir), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.home_dir).st_mode), 0o700)
        with open(manifest) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": GEN,
                    "image": IMAGE,
                    "cwd": self.cwd,
                    "agent_dir": self.home_dir,
                    "hook_path": os.path.join(REPO, "bin", "agent-pm-session"),
                    "labels": {},
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(manifest).st_mode), 0o600)
        self.assertEqual(
            self.host_record(),
            {
                "generation": GEN,
                "sid": "s1",
                "client": "agent",
                "display": "lead/below",
                "record": RECORD.to_line(),
                "hidden": False,
                "process": PROCESS,
            },
        )
        self.assertIsInstance(process, DockerTerminal)
        self.assertEqual((process.identity, process.token), (PROCESS, None))

    def test_the_container_launch_and_the_supervisor(self):
        self.start()
        self.assertEqual(self.docker.execs()[0], ["test", "-S", SOCKET])
        new = self.docker.execs()[1]
        tail = new[new.index("--") :]
        self.assertEqual(tail[:4], ["--", "with-secrets", "agent", "--flag"])
        self.assertEqual(new[new.index("-c") + 1], self.cwd)
        self.assertIn(f"AGENT_HOME={self.home_dir}", new)
        self.assertIn("K=v", new)
        self.assertFalse(any(arg.startswith("PATH=") or "/host/bin" in arg for arg in new))
        for tag in ("@kind docker", f"@generation {GEN}", "@sid s1"):
            self.assertIn(tag, " ".join(new))
        self.assertIn("set-option @state dead ; wait-for -S agent-pm-dead", new)
        self.assertNotIn("@record", " ".join(new))
        argv = self.popen_mock.call_args.args[0]
        self.assertEqual(argv[1:5], ["-P", "-s", "-m", "agent_pm.session.hosts.docker.supervisor"])
        self.assertEqual(
            argv[5:10],
            [
                "w",
                self.state,
                os.path.join(self.home, ".agent-pm", "tui", "w"),
                os.path.join(self.home, "events"),
                os.path.join(REPO, "bin", "agent-pm-session"),
            ],
        )
        relay, wait, check = (json.loads(arg) for arg in argv[10:])
        prefix = [DOCKER, "--host", self.ep, "exec"]
        self.assertEqual(relay, [*prefix, "-i", CONTAINER, *RELAY])
        self.assertEqual(RELAY[0], "/opt/agent-pm/venv/bin/python")
        self.assertTrue(RELAY[-1].endswith("/agent_pm/session/hosts/docker/relay.py"))
        self.assertEqual(wait, [*prefix, CONTAINER, "tmux", "wait-for", "agent-pm-dead"])
        self.assertEqual(
            check,
            [*prefix, CONTAINER, "tmux", "display-message", "-p", "-t", "=w:", "#{pane_dead}"],
        )
        kw = self.popen_mock.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertFalse(set(CLEARED) & kw["env"].keys())

    def test_resume_keeps_the_home_and_starts_a_new_container(self):
        transcript = self.history()
        process = self.start(resume="1")
        with open(transcript) as f:
            self.assertEqual(f.read(), "{}\n")
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertIn(("start", manifest, client_keys(REPO)), self.order)
        with open(manifest) as f:
            found = json.load(f)
        self.assertEqual((found["cwd"], found["agent_dir"]), (self.cwd, self.home_dir))
        self.assertIsInstance(process, DockerTerminal)

    def test_resume_without_native_history_fails_before_the_broker(self):
        missing = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        outside = os.path.join(self.cwd, "s1.jsonl")
        with open(outside, "w") as f:
            f.write("{}\n")
        for case, transcript, keep in (
            ("no home", missing, False),
            ("no transcript", missing, True),
            ("outside the home", outside, True),
            ("none named", "", True),
        ):
            with self.subTest(case=case):
                if keep:
                    os.makedirs(self.home_dir, exist_ok=True)
                    with open(os.path.join(self.home_dir, "keep"), "w"):
                        pass
                self.launch = replace(self.launch, transcript=transcript)
                self.refused_untouched(
                    ConfigError, f"docker session w: no native history to resume in {self.home_dir}"
                )
                if keep:
                    self.assertEqual(os.listdir(self.home_dir), ["keep"])

    def test_resume_through_a_link_fails_before_the_broker(self):
        secret = os.path.join(self.account, "secret.jsonl")
        with open(secret, "w") as f:
            f.write("host only\n")
        transcript = self.history()
        os.remove(transcript)
        os.symlink(secret, transcript)
        self.refused_untouched(ConfigError, "no native history")
        project = os.path.dirname(transcript)
        os.remove(transcript)
        os.rmdir(project)
        elsewhere = os.path.join(self.account, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(secret, os.path.join(elsewhere, "s1.jsonl"))
        os.symlink(elsewhere, project)
        self.refused_untouched(ConfigError, "no native history")

    def test_a_missing_or_bad_resume_flag_is_refused_untouched(self):
        os.makedirs(self.home_dir)
        with open(os.path.join(self.home_dir, "keep"), "w"):
            pass
        for flag in (None, "", "yes", "2"):
            with self.subTest(flag=flag):
                self.order.clear()
                with self.assertRaisesRegex(SessionError, "docker session w: bad resume flag"):
                    self.start(resume=flag)
                self.assertEqual(self.order, [])
                self.assertEqual(os.listdir(self.home_dir), ["keep"])

    def test_each_start_writes_its_generation_result(self):
        self.broker.started = Started(
            CONTAINER, self.ep, "sha256:" + "b" * 64, {"claude": "2.1.0"}, {"status": "updated"}
        )
        result = os.path.join(self.state, f"{GEN}.result.json")
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                    os.remove(result)
                self.start(resume=resume)
                with open(result) as f:
                    found = json.load(f)
                began, ended = found.pop("started_at"), found.pop("ended_at")
                self.assertLessEqual(parse_time(began), parse_time(ended))
                self.assertEqual(
                    found,
                    {
                        "container": CONTAINER,
                        "image": IMAGE,
                        "image_digest": "sha256:" + "b" * 64,
                        "client_versions": {"claude": "2.1.0"},
                        "update": {"status": "updated"},
                    },
                )
                self.assertFalse(result.startswith(self.home_dir + os.sep))

    def test_each_start_freezes_its_plan_before_the_broker_start(self):
        self.launch = replace(self.launch, env={**self.launch.env, "TOKEN": "sk-secret"})
        plan = os.path.join(self.state, f"{GEN}.plan.json")
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                    os.remove(plan)
                self.broker.error = BrokerError(5, "login_required", "log in", "not_needed", None)
                with (
                    mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    self.assertRaises(BrokerError),
                ):
                    self.start(resume=resume)
                with open(plan) as f:
                    text = f.read()
                self.assertEqual(
                    json.loads(text),
                    {
                        "name": "w",
                        "generation": GEN,
                        "image": IMAGE,
                        "image_digest": DIGEST,
                        "mounts": [
                            {"path": self.cwd, "rw": True},
                            {"path": self.home_dir, "rw": True},
                        ],
                        "credential": "agent-token",
                    },
                )
                head = f"docker plan w {GEN}:"
                self.assertEqual(
                    stderr.getvalue().splitlines(),
                    [
                        f"{head} image {IMAGE} {DIGEST}",
                        f"{head} mounts {self.cwd} rw, {self.home_dir} rw",
                        f"{head} credential agent-token",
                    ],
                )
                for absent in ("sk-secret", "--flag", "approval"):
                    self.assertNotIn(absent, text + stderr.getvalue())
                self.assertFalse(plan.startswith(self.home_dir + os.sep))

    def test_a_plan_write_failure_starts_nothing(self):
        os.makedirs(os.path.join(self.state, f"{GEN}.plan.json", "taken"))
        with self.assertRaisesRegex(SessionError, "docker session w: plan: "):
            self.start()
        self.assertFalse(any(isinstance(step, tuple) and step[0] == "start" for step in self.order))

    def test_bad_plan_metadata_is_refused_untouched(self):
        for changes, pattern in (
            ({"credential": None}, "no client home"),
            ({"credential": ""}, "no client home"),
        ):
            with self.subTest(changes=changes):
                self.order.clear()
                with self.assertRaisesRegex(SessionError, f"docker session w: {pattern}"):
                    self.start(**changes)
                self.assertEqual(self.order, [])

    def test_a_result_write_failure_rolls_back(self):
        os.makedirs(os.path.join(self.state, f"{GEN}.result.json", "taken"))
        with self.assertRaisesRegex(SessionError, "docker session w: "):
            self.start()
        self.supervisor.terminate.assert_called()
        self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_existing_container_refuses_start(self):
        self.docker.ps = ["agent-pm-w-other\tw\tother"]
        with self.assertRaisesRegex(SessionError, "a container for it exists"):
            self.start()
        self.assertEqual(len(self.order), 1)

    def test_bad_cwd_fails_before_any_docker_call(self):
        self.launch = Launch(("agent",), cwd=self.account, env={})
        with self.assertRaisesRegex(ConfigError, f"Docker cwd {self.account}"):
            self.start()
        self.assertEqual(self.order, [])

    def test_failure_after_start_rolls_back(self):
        for replies in (
            {("tmux", "new-session"): (1, "", "boom")},
            {("test",): (1, "", "")},
            {
                ("tmux", "new-session"): (0, PROCESS, ""),
                ("tmux", "display-message"): (0, "1 1 \n", ""),
            },
        ):
            with self.subTest(replies=replies), mock.patch("time.sleep"):
                self.docker.replies = replies
                self.order.clear()
                with (
                    mock.patch("time.monotonic", side_effect=range(0, 10000, 5)),
                    self.assertRaisesRegex(SessionError, "docker session w: "),
                ):
                    self.start()
                self.supervisor.terminate.assert_called()
                self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_a_foreign_container_or_endpoint_rolls_back(self):
        for changes in (
            {"container": f"agent-pm-x-{GEN}"},
            {"endpoint": "unix:///x/.docker/run/docker.sock"},
        ):
            with self.subTest(changes=changes):
                self.broker.started = Started(**{**vars(self.broker.started), **changes})
                with self.assertRaisesRegex(SessionError, "broker: started"):
                    self.start()
                self.assertEqual(self.order[-1], ("close", CONTAINER))
                self.assertNotIn("supervisor", self.order)

    def test_cleanup_failed_retries_close(self):
        self.broker.error = BrokerError(9, "start_failed", "no", "failed", CONTAINER)
        with self.assertRaises(BrokerError):
            self.start()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_login_required_surfaces_message(self):
        self.broker.error = BrokerError(5, "login_required", "log in first", "not_needed", None)
        with self.assertRaisesRegex(SessionError, "broker: log in first"):
            self.start()
        self.assertEqual(self.broker.closed, [])
        self.assertEqual(sum(isinstance(s, tuple) and s[0] == "start" for s in self.order), 1)
        self.popen_mock.assert_not_called()

    def test_no_client_home_is_refused(self):
        with self.assertRaisesRegex(SessionError, "no client home"):
            self.start(home_dir="")

    def test_detach_is_refused(self):
        with self.assertRaisesRegex(SessionError, "only terminal sessions"):
            self.host.detach("w", self.launch)


class PreflightTest(Base):
    def validate(self):
        self.host.validate("w", Launch(("agent",), cwd=self.cwd, env={}))

    def test_preflight_runs_version_then_build_and_writes_nothing(self):
        self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertFalse(os.path.exists(self.state))

    def test_a_failed_handshake_stops_before_build(self):
        self.broker.failing["version"] = SessionError("broker: protocol '1', want '2'")
        with self.assertRaisesRegex(SessionError, "protocol '1', want '2'"):
            self.validate()
        self.assertEqual(self.order, ["version"])

    def test_a_failed_build_stops_before_any_file(self):
        self.broker.failing["build"] = BrokerError(
            8, "build_failed", "no image", "not_needed", None
        )
        with self.assertRaisesRegex(SessionError, "broker: no image"):
            self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertFalse(os.path.exists(self.state))

    def test_a_missing_cwd_is_refused_before_the_broker(self):
        self.cwd = os.path.join(self.account, "src", "gone")
        with self.assertRaisesRegex(ConfigError, f"Docker cwd {self.cwd}: no such directory"):
            self.validate()
        self.assertEqual(self.order, [])


class TerminalTest(Base):
    def test_session_commands_run_through_docker_exec(self):
        self.docker.replies = {
            ("tmux", "display-message"): pane(),
            ("tmux", "capture-pane"): (0, "hello\n\n", ""),
        }
        process = self.terminal()
        process.send(Paste("hi"))
        process.press([KeyPress("a")])
        self.assertEqual(self.host.screen(process), "hello")
        self.assertEqual(self.host.screen("w"), "hello")
        execs = self.docker.execs()
        self.assertTrue(all(call[0] == "tmux" for call in execs))
        commands = [call[1] for call in execs]
        for command in ("load-buffer", "copy-mode", "capture-pane"):
            self.assertIn(command, commands)
        self.assertIn("hi", self.docker.inputs)
        self.assertTrue(
            all(c[:3] == ["exec", "-i", CONTAINER] for c in self.order if c[0] == "exec")
        )
        self.assertEqual(self.host_calls, [])

    def test_dead_pane_returncode(self):
        process = self.terminal()
        for dead, code in (("1 3 ", 3), ("1  9", -9), ("1  kill", -signal.SIGKILL)):
            with self.subTest(dead=dead):
                self.docker.replies = {("tmux", "display-message"): pane(dead)}
                self.assertFalse(process.alive())
                self.assertEqual(process.returncode(), code)

    def test_missing_container_is_absent(self):
        process = self.terminal()
        for reply in (
            (1, "", f"Error response from daemon: No such container: {CONTAINER}\n"),
            (1, "", f"Error response from daemon: container {CONTAINER} is not running\n"),
            TimeoutExpired("docker", 10),
            Overflow("output over 8388608 bytes"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {(): reply}
                self.assertEqual(process.state(), Liveness.ABSENT)
                self.assertFalse(process.alive())
                self.assertEqual(process.status().state, StatusState.DEAD)

    def test_terminal_never_reads_container_control(self):
        self.terminal()
        self.assertEqual(self.docker.execs(), [])

    def test_kill_signals_the_pane_group(self):
        process = self.terminal()
        self.docker.replies = {
            ("tmux", "display-message"): pane("1 0 "),
            ("kill", "-0"): (1, "", ""),
        }
        process.kill()
        self.assertEqual(self.docker.execs()[0], ["kill", "-KILL", "--", "-4343"])
        self.assertIn(["kill", "-0", "--", "-4343"], self.docker.execs())

    def test_record_and_display_ignore_container_options(self):
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        self.assertEqual(
            (process.display(), process.session_identity(), process.hidden(), process.record()),
            ("lead/below", ("s1", GEN), False, None),
        )
        record = RECORD
        process.publish(record)
        process.mark_hidden(hidden=True)
        self.assertEqual((process.record(), process.hidden()), (record, True))
        self.assertFalse(any("show-options" in call for call in self.docker.execs()))
        self.record(generation="f" * 32)
        self.assertEqual(
            (process.display(), process.session_identity(), process.record()),
            (None, (None, None), None),
        )
        with self.assertRaisesRegex(SessionError, "is gone"):
            process.publish(record)

    def test_mark_names_the_host_session(self):
        events = os.path.join(self.home, "events")
        open(events, "w").close()
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        process.mark(StatusState.DONE, events)
        self.assertIn(["tmux", "set-option", "-t", "%7", "@state", "done"], self.docker.execs())
        self.assertFalse(any("#S" in call for call in self.docker.execs()))
        with open(events) as f:
            self.assertRegex(f.read(), r"^\d\d:\d\d:\d\d w done\n$")

    def test_status_attach_is_the_docker_command(self):
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        want = f"{DOCKER} --host {self.ep} exec -it {CONTAINER} tmux attach -t =w"
        self.assertEqual(process.status().attach, want)
        self.assertEqual(self.host.attach_command("w"), want)


class CloseTest(Base):
    def hold_lock(self):
        lock = os.path.join(self.state, "supervisor.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with open(os.path.join(self.state, "supervisor.pid"), "w") as f:
            f.write("999")
        return fd

    def test_close_order(self):
        process = self.terminal()
        fd = self.hold_lock()
        self.host_replies = {("show-options",): (0, "docker\n", ""), ("kill-session",): (0, "", "")}

        def kill(pid, sig):
            self.order.append(("signal", pid, sig))
            os.close(fd)

        with mock.patch("os.kill", side_effect=kill):
            process.close()
        self.assertEqual(
            [step[:4] if isinstance(step, list) else step for step in self.order[1:]],
            [
                ["exec", "-i", CONTAINER, "tmux"],
                ("signal", 999, signal.SIGTERM),
                ("close", CONTAINER),
            ],
        )
        self.assertEqual(
            self.docker.execs()[0],
            [
                "tmux",
                "set-hook",
                "-p",
                "-u",
                "-t",
                "%7",
                "pane-died",
                ";",
                "kill-session",
                "-t",
                "=w",
            ],
        )
        self.assertEqual(self.host_calls[-1], ["kill-session", "-t", "=w"])
        self.assertTrue(os.path.isdir(self.state))

    def test_close_when_container_gone(self):
        process = self.terminal()
        self.docker.replies = {
            (): (1, "", f"Error response from daemon: No such container: {CONTAINER}")
        }
        with mock.patch("os.kill") as kill:
            process.close()
        kill.assert_not_called()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_supervisor_that_keeps_its_lock_fails_close_after_the_rest(self):
        process = self.terminal()
        self.hold_lock()
        with (
            mock.patch("os.kill") as kill,
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", side_effect=range(0, 10000, 10)),
            self.assertRaisesRegex(SessionError, "supervisor 999: still holds"),
        ):
            process.close()
        self.assertEqual(
            [c.args for c in kill.call_args_list], [(999, signal.SIGTERM), (999, signal.SIGKILL)]
        )
        self.assertEqual(self.broker.closed, [CONTAINER])


class ListTest(Base):
    def test_list_and_rows_from_labelled_containers(self):
        self.listed("w", "v")
        self.docker.ps += [
            f"agent-pm-x-{GEN}\tnot a name\t{GEN}",
            f"someone-else\tz\t{GEN}",
            "bad line",
        ]
        self.record()
        self.assertEqual(self.host.list(), ["v", "w"])
        self.assertEqual(self.host.list("lead"), ["w"])
        self.assertEqual(self.host.list(""), [])
        listing = "w\t0\tforged\tforged\tdone\t\t\tforged\t\n"
        self.docker.replies = {("tmux", "-u", "list-sessions"): (0, listing, "")}
        before = len(self.docker.order)
        rows = {row.name: row for row in self.host.rows()}
        ran = [call[0] for call in self.docker.order[before:] if isinstance(call, list)]
        self.assertEqual(ran.count("ps"), 1)
        self.assertEqual(
            (rows["w"].sid, rows["w"].client, rows["w"].display, rows["w"].state),
            ("s1", "agent", "lead/below", StatusState.DONE),
        )
        self.assertEqual((rows["v"].sid, rows["v"].state), (None, StatusState.DEAD))

    def test_rows_ignore_other_container_sessions_and_bad_labels(self):
        self.terminal()
        for listing in ("x\t0\t\t\t\t\t\t\t\n", "w\t0\t\t\t\t\t\t\t\nw\t0\t\t\t\t\t\t\t\n", ""):
            with self.subTest(listing=listing):
                self.docker.replies = {("tmux", "-u", "list-sessions"): (0, listing, "")}
                (row,) = self.host.rows()
                self.assertEqual((row.name, row.state), ("w", StatusState.DEAD))
        self.docker.replies = {(): (1, "", "Error response from daemon: No such container: x")}
        self.assertEqual(self.host.rows()[0].state, StatusState.DEAD)

    def test_docker_ps_failure_raises(self):
        for run in (
            mock.Mock(return_value=done(1, "", "Cannot connect")),
            mock.Mock(side_effect=PermissionError(13, "denied")),
        ):
            with self.subTest(run=run):
                self.host.run = run
                with self.assertRaisesRegex(SessionError, "docker ps"):
                    self.host.list()

    def test_no_daemon_socket_or_cli_lists_nothing(self):
        self.listed("w")
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "no docker"))
        self.assertEqual(self.host.list(), [])
        os.unlink(self.ep.removeprefix("unix://"))
        self.host.run = self.docker
        self.assertEqual((self.host.list(), self.host.rows(), self.order), ([], [], []))

    def test_attach_unknown_or_without_client(self):
        with self.assertRaisesRegex(SessionError, "no docker session w"):
            self.host.attach("w")
        self.listed("w")
        self.docker.replies = {("tmux", "display-message"): (1, "", "can't find session: =w")}
        with self.assertRaisesRegex(SessionError, "runs no client"):
            self.host.attach("w")
        self.docker.replies = {("tmux", "display-message"): (0, PROCESS + "\n", "")}
        self.assertEqual(self.host.attach("w").identity, PROCESS)

    def test_hook_callback(self):
        callback = self.host.hook_callback("w", {"generation": GEN})
        self.assertEqual(callback.module, "agent_pm.session.hosts.tmux.docker_hook")
        self.assertEqual(
            dict(callback.state),
            {
                "docker": DOCKER,
                "endpoint": self.ep,
                "container": CONTAINER,
                "target": "w",
                "generation": GEN,
            },
        )


class ShowTest(Base):
    def setUp(self):
        super().setUp()
        self.terminal()
        self.host.view = mock.Mock()
        self.host.view.show.return_value = None
        self.host.view.placement.shown.return_value = "%9"

    def test_show_creates_view_and_places_it(self):
        self.host_replies = {("new-session",): (0, "", "")}
        self.assertIsNone(self.host.show("w", display="lead/below"))
        new = self.host_calls[-1]
        self.assertEqual(new[:5], ["new-session", "-d", "-s", "w", "--"])
        self.assertIn(
            f"{DOCKER} --host {self.ep} exec -it {CONTAINER} tmux attach -t =w", " ".join(new)
        )
        self.assertEqual(new[-4:], ["-t", "=w:", "@view", "docker"])
        for key in CLEARED:
            self.assertIn(key, new)
        self.host.view.show.assert_called_once_with("w", display="lead/below", command=None)
        self.assertFalse(self.host_record()["hidden"])

    def test_show_refuses_a_foreign_host_session(self):
        self.host_replies = {
            ("display-message",): (0, "0  \n", ""),
            ("show-options",): (1, "", "invalid option: @view"),
        }
        with self.assertRaisesRegex(SessionError, "a host tmux session w exists"):
            self.host.show("w")
        self.host.view.show.assert_not_called()

    def test_a_failed_show_returns_the_attach_command(self):
        self.host_replies = {
            ("display-message",): (0, "0  \n", ""),
            ("show-options",): (0, "docker\n", ""),
        }
        self.host.view.show.return_value = "tmux attach -t '=w'"
        self.host.view.placement.shown.return_value = None
        self.assertEqual(self.host.show("w"), self.host.attach_command("w"))
        self.assertTrue(self.host_record()["hidden"])

    def test_hide_marks_hidden(self):
        self.host.view.placement.shown.return_value = None
        self.host.hide("w")
        self.host.view.hide.assert_not_called()
        self.assertTrue(self.host_record()["hidden"])
        self.host_replies = {("display-message",): (0, "0  \n", "")}
        self.host.hide("w", if_closed=True)
        self.host.view.hide.assert_called_once_with("w", if_closed=True)

    def test_display_layout_and_tile_are_the_tmux_hosts(self):
        for method, args, kw in (
            ("display", ("x",), {}),
            ("display_help", (), {}),
            ("layout", (), {"display": "x"}),
            ("tile", ("@1",), {"wait": True, "if_panes_changed": False}),
        ):
            with self.subTest(method=method):
                getattr(self.host, method)(*args, **kw)
                getattr(self.host.view, method).assert_called_once_with(*args, **kw)
