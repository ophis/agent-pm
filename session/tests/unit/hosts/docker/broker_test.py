"""hosts/docker/broker.py: protocol-1 calls, every shared response fixture's outcome, the manifest."""

import json
import os
import stat
import tempfile

from agent_pm.session.engine.types import ConfigError, SessionError
from agent_pm.session.hosts.docker import broker
from agent_pm.session.hosts.docker.broker import BROKER, Broker, BrokerError
from agent_pm.session.hosts.docker.types import Started

from session.tests.case import REPO, Case
from session.tests.unit.hosts.docker.support import (
    HOOK,
    FakeBroker,
    cases,
    data,
    failure,
    success,
)
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired

MANIFEST = "/Users/example/.agent-pm/docker-state/worker_1/0123456789abcdef0123456789abcdef.json"
CONTAINER = "agent-pm-worker_1-0123456789abcdef0123456789abcdef"
KEYS = broker.client_keys(REPO)
CALLS = {
    "version_output": ("version", lambda b: b.version()),
    "build_output": ("build", lambda b: b.build("/opt/agent-pm")),
    "start_output": ("start", lambda b: b.start(MANIFEST, clients=KEYS)),
    "close_output": ("close", lambda b: b.close(CONTAINER)),
}
COMMANDS = {
    "version": CALLS["version_output"],
    "build": CALLS["build_output"],
    "start": CALLS["start_output"],
    "close": CALLS["close_output"],
    "unknown": CALLS["version_output"],
}


def argv(command):
    return [BROKER, *data(f"{command}-input.json")["argv"]]


class PluginRootTest(Case):
    def test_plugin_root_from_hook_path(self):
        self.assertEqual(broker.plugin_root(HOOK), REPO)
        self.assertEqual(KEYS, frozenset({"claude", "codex"}))
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            with self.assertRaisesRegex(ConfigError, "protocol.schema.json"):
                broker.plugin_root(os.path.join(tmp, "bin", "agent-pm-session"))


class ResponseTest(Case):
    def test_every_response_fixture_has_its_outcome(self):
        found = cases()
        self.assertTrue(any(c["definition"] == "error" for c in found))
        for case in (c for c in found if c["expected"]["stage"] == "response"):
            name, definition, valid = case["file"], case["definition"], case["valid"]
            with self.subTest(name=name):
                if definition == "error":
                    command, call = COMMANDS[data(name)["error"]["command"]]
                    fake = FakeBroker(failure(name))
                else:
                    command, call = CALLS[definition]
                    fake = FakeBroker(success(name))
                if definition == "error" and valid:
                    with self.assertRaises(BrokerError) as raised:
                        call(Broker(fake))
                    error = data(name)
                    self.assertEqual(
                        (raised.exception.exit_code, raised.exception.code),
                        (error["exit_code"], error["error"]["code"]),
                    )
                    self.assertEqual(raised.exception.cleanup, error["error"]["cleanup"])
                    self.assertEqual(raised.exception.container, error["error"].get("container"))
                    self.assertIn(error["error"]["message"], str(raised.exception))
                elif valid:
                    call(Broker(fake))
                else:
                    with self.assertRaisesRegex(SessionError, "broker: bad response") as raised:
                        call(Broker(fake))
                    self.assertNotIsInstance(raised.exception, BrokerError)
                self.assertEqual(fake.calls, [argv(command)])
                self.assertEqual(
                    fake.kwargs[0],
                    {
                        "capture_output": True,
                        "text": True,
                        "stdin": DEVNULL,
                        "env": {"PATH": os.defpath},
                        "timeout": broker.TIMEOUT,
                    },
                )

    def test_parsed_successes(self):
        self.assertEqual(
            Broker(FakeBroker(success("build-cached.json"))).build("/opt/agent-pm"),
            (data("build-cached.json")["image"], data("build-cached.json")["digest"]),
        )
        started = Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=KEYS)
        self.assertEqual(
            started,
            Started(
                container=CONTAINER,
                endpoint="unix:///Users/example/.docker/run/docker.sock",
                image_digest="sha256:" + "b" * 64,
                clients={"claude": "2.1.0", "codex": "0.100.0"},
                update={"status": "updated"},
            ),
        )
        self.assertFalse(Broker(FakeBroker(success("close-absent.json"))).close(CONTAINER))

    def test_mismatched_exit_is_transport_failure(self):
        for result in (
            failure("error-login_required.json", rc=9),
            (0, "", ""),
            (0, "not json\n", ""),
            (0, '{"protocol_version": "2", "protocol_version": "2"}\n', ""),
            (0, '{"protocol_version": NaN}\n', ""),
            (0, '{"protocol_version": "2"} {}\n', ""),
            (5, "", "not json\n"),
            (5, "", '{"exit_code": true, "error": {}}\n'),
            (
                5,
                "",
                '{"exit_code": 5, "error": {"command": [], "code": [], "message": "m",'
                ' "cleanup": []}}\n',
            ),
            TimeoutExpired(BROKER, broker.TIMEOUT),
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(SessionError, "broker: ") as raised:
                    Broker(FakeBroker(result)).version()
                self.assertNotIsInstance(raised.exception, BrokerError)

    def test_wrong_protocol_or_missing_broker_fails_before_build(self):
        for result in (
            (0, '{"protocol_version": "1"}\n', ""),
            FileNotFoundError(2, "No such file", BROKER),
            PermissionError(13, "Permission denied", BROKER),
        ):
            with self.subTest(result=result):
                fake = FakeBroker(result)
                with self.assertRaisesRegex(SessionError, "docs/docker-host.md") as raised:
                    Broker(fake).version()
                self.assertIn(BROKER, str(raised.exception))
                self.assertEqual(fake.calls, [argv("version")])

    def test_an_unhashable_update_reason_is_bad(self):
        started = data("start-fallback-timeout.json")
        started["update"]["reason"] = []
        result = (0, json.dumps(started), "")
        with self.assertRaisesRegex(SessionError, "bad response"):
            Broker(FakeBroker(result)).start(MANIFEST, clients=KEYS)

    def test_a_client_key_set_other_than_the_schemas_is_bad(self):
        for keys in (frozenset({"claude"}), frozenset({"claude", "codex", "other"})):
            with self.subTest(keys=keys), self.assertRaisesRegex(SessionError, "bad response"):
                Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=keys)


class ManifestTest(Case):
    def test_writes_the_protocol_manifest_owner_only(self):
        path = os.path.join(self.home, "state", "g.json")
        broker.write_manifest(
            path,
            name="w",
            generation="0" * 32,
            image="agent-pm-worker:" + "a" * 64,
            cwd="/work",
            home=("x_dir", "/home/x"),
            hook_path="/plugin/bin/agent-pm-session",
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": "0" * 32,
                    "image": "agent-pm-worker:" + "a" * 64,
                    "cwd": "/work",
                    "x_dir": "/home/x",
                    "hook_path": "/plugin/bin/agent-pm-session",
                    "labels": {},
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
