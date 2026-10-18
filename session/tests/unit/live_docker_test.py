"""The live Docker gate's checks, fed recorded shapes of the audit log, docker inspect, results and PRs."""

import json
import os
import re
import unittest

from session.tests import live_docker as gate

G1, G2 = "a" * 32, "b" * 32
FAKE = "ghs_" + "x" * 36
CWD = "/Users/u/work/live"
CLAUDE_DIR = "/Users/u/.agent-pm/docker-state/live/claude"
DOCS = os.path.join(os.path.dirname(gate.__file__), "..", "..", "docs", "credentials.md")


def token(result, account="ophis", installation=1, **extra):
    return {
        "kind": "github-installation-token",
        "result": result,
        "account": account,
        "installation": installation,
        "expires_at": "2026-10-18T13:00:00Z",
        **extra,
    }


def line(event, generation=G1, *credentials, time="2026-10-18T12:00:00.000000001Z"):
    return {
        "time": time,
        "command": event,
        "event": event,
        "session": "live",
        "generation": generation,
        "container": f"agent-pm-live-{generation}",
        "credentials": list(credentials),
    }


def withdrawn(kind="claude-setup-token"):
    return {"kind": kind, "result": "withdrawn"}


def inspect(**changes):
    found = {
        "Config": {"User": "agent", "Env": [f"GH_TOKEN={FAKE}"]},
        "HostConfig": {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            "NetworkMode": "bridge",
            "PidMode": "",
            "IpcMode": "private",
        },
        "Mounts": [
            {"Type": "volume", "Name": "agent-pm-ws-live", "Destination": CWD},
            {"Type": "bind", "Source": CLAUDE_DIR, "Destination": CLAUDE_DIR},
            {"Type": "tmpfs", "Destination": "/run/agent-pm/secrets"},
        ],
    }
    for key, value in changes.items():
        part, _, field = key.partition("__")
        if field:
            found[part][field] = value
        else:
            found[part] = value
    return found


def view(*, login="app/agent-pm-bot", draft=True, created="2026-10-18T12:10:00Z"):
    return {"author": {"login": login}, "isDraft": draft, "createdAt": created, "number": 7}


class AuditLinesTest(unittest.TestCase):
    def test_skips_blank_and_malformed_lines(self):
        text = "\n".join([json.dumps(line("start")), "", "{oops", "[1]", json.dumps(line("close"))])
        self.assertEqual([x["event"] for x in gate.audit_lines(text)], ["start", "close"])


class ReclaimedTest(unittest.TestCase):
    def test_revoked_and_withdrawn(self):
        lines = [
            line("start", G1, token("issued")),
            line("close", G1, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_codex_withdrawal(self):
        lines = [line("close", G1, token("revoked"), withdrawn("codex-access-token"))]
        self.assertEqual(gate.reclaimed(lines, G1, "codex"), [])
        self.assertEqual(len(gate.reclaimed(lines, G1, "claude")), 1)

    def test_pending_names_its_reason(self):
        lines = [line("close", G1, token("pending", reason="network down"), withdrawn())]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("pending", problem)
        self.assertIn("network down", problem)

    def test_expired_unrevoked(self):
        lines = [
            line("close", G1, token("pending", reason="offline"), withdrawn()),
            line("revocation-retry", G1, token("expired-unrevoked")),
        ]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("expired-unrevoked", problem)

    def test_pending_then_revoked_by_retry(self):
        lines = [
            line("close", G1, token("pending", reason="offline"), withdrawn()),
            line("revocation-retry", G1, token("revoked")),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_every_installation_counts(self):
        lines = [line("close", G1, token("revoked"), token("pending", "other", 2), withdrawn())]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("other", problem)

    def test_no_close_line(self):
        (problem,) = gate.reclaimed([line("start", G1, token("issued"))], G1, "claude")
        self.assertIn("no close", problem)

    def test_no_github_token(self):
        (problem,) = gate.reclaimed([line("close", G1, withdrawn())], G1, "claude")
        self.assertIn("github-installation-token", problem)

    def test_no_withdrawal(self):
        (problem,) = gate.reclaimed([line("close", G1, token("revoked"))], G1, "claude")
        self.assertIn("withdrawn", problem)

    def test_other_generation_ignored(self):
        lines = [
            line("close", G2, token("pending", reason="offline")),
            line("close", G1, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_role_generation(self):
        lines = [
            line("start", G2, token("issued")),
            line("refresh", G2, token("renewed")),
            line("close", G2, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G2, "claude"), [])


class RemovedTokensTest(unittest.TestCase):
    def test_revoked_later(self):
        lines = [line("start", G1, token("issued")), line("close", G1, token("revoked"))]
        self.assertEqual(gate.removed_tokens(lines, G1), f"removed {G1}: github tokens revoked 1/1")

    def test_never_revoked(self):
        lines = [line("start", G1, token("issued"), token("issued", "other", 2))]
        self.assertEqual(
            gate.removed_tokens(lines, G1),
            f"removed {G1}: github tokens not revoked (revoked 0/2), expires 2026-10-18T13:00:00Z",
        )


class BaselineTest(unittest.TestCase):
    def check(self, found, plan=(CLAUDE_DIR,)):
        return gate.baseline(found, "live", CWD, list(plan))

    def test_compliant_claude(self):
        self.assertEqual(self.check(inspect()), [])

    def test_compliant_codex(self):
        sessions = "/Users/u/.agent-pm/docker-state/live/codex/sessions"
        mounts = [
            {"Type": "volume", "Name": "agent-pm-ws-live", "Destination": CWD},
            {"Type": "tmpfs", "Destination": "/opt/agent-pm/runtime"},
            {"Type": "bind", "Source": sessions, "Destination": sessions},
        ]
        self.assertEqual(self.check(inspect(Mounts=mounts), (sessions,)), [])

    def test_no_new_privileges_with_value(self):
        self.assertEqual(
            self.check(inspect(HostConfig__SecurityOpt=["no-new-privileges:true"])), []
        )

    def test_each_violation_alone(self):
        volume = {"Type": "volume", "Name": "agent-pm-ws-live", "Destination": CWD}
        bind = {"Type": "bind", "Source": CLAUDE_DIR, "Destination": CLAUDE_DIR}
        cases = {
            "cap": inspect(HostConfig__CapDrop=["NET_RAW"]),
            "no-new-privileges": inspect(HostConfig__SecurityOpt=[]),
            "user": inspect(Config__User="root"),
            "privileged": inspect(HostConfig__Privileged=True),
            "network": inspect(HostConfig__NetworkMode="host"),
            "pid": inspect(HostConfig__PidMode="host"),
            "ipc": inspect(HostConfig__IpcMode="host"),
            "extra volume": inspect(
                Mounts=[volume, bind, {"Type": "volume", "Name": "x", "Destination": "/x"}]
            ),
            "wrong volume target": inspect(Mounts=[{**volume, "Destination": "/elsewhere"}, bind]),
            "bind not in plan": inspect(
                Mounts=[volume, bind, {"Type": "bind", "Source": "/opt/x", "Destination": "/opt/x"}]
            ),
        }
        for label, found in cases.items():
            with self.subTest(label):
                self.assertEqual(len(self.check(found)), 1, self.check(found))

    def test_cwd_related_binds(self):
        volume = {"Type": "volume", "Name": "agent-pm-ws-live", "Destination": CWD}
        for source in (CWD, "/Users/u/work", CWD + "/src"):
            with self.subTest(source):
                found = inspect(
                    Mounts=[volume, {"Type": "bind", "Source": source, "Destination": "/w"}]
                )
                problems = gate.baseline(found, "live", CWD, [source])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("cwd", problems[0])

    def test_docker_socket(self):
        volume = {"Type": "volume", "Name": "agent-pm-ws-live", "Destination": CWD}
        sock = "/var/run/docker.sock"
        found = inspect(Mounts=[volume, {"Type": "bind", "Source": sock, "Destination": sock}])
        problems = gate.baseline(found, "live", CWD, [sock])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("docker.sock", problems[0])


class ZeroCountsTest(unittest.TestCase):
    def test_counts(self):
        self.assertTrue(gate.zero_counts("0\n0\n"))
        self.assertTrue(gate.zero_counts("      0\n\n0\n"))
        self.assertFalse(gate.zero_counts("0\n12\n"))
        self.assertFalse(gate.zero_counts(""))


class TimingTest(unittest.TestCase):
    PHASES = {"checks": 0.1, "image": 0.5, "start": 4.0, "relay": 0.3, "tmux": 0.2, "client": 1.0}
    STDERR = (
        "docker timing live: preflight 2.40 s\n"
        "agent-pm-broker: start preflight 0.01 s\n"
        "agent-pm-broker: start client_update 3.25 s\n"
        f"agent-pm-broker: start credentials 0.10 s {FAKE}\n"
    )

    def result(self, **extra):
        return {"phases": self.PHASES, "launched_at": "2026-10-18T14:00:00.000+02:00", **extra}

    def hooks(self, *stamps):
        return "".join(
            json.dumps({"ts": ts, "label": "Stop", "payload": {}}) + "\n" for ts in stamps
        )

    def test_claude_terminal(self):
        hooks = self.hooks("2026-10-18T13:59:59.000+02:00", "2026-10-18T14:00:02.500+02:00")
        self.assertEqual(
            gate.timing(self.result(), hooks, self.STDERR, "live"),
            "checks 0.1 s | image 0.5 s | start 4.0 s | relay 0.3 s | tmux 0.2 s | client 1.0 s"
            " | client ready 2.5 s | preflight 2.4 s",
        )

    def test_codex_client_update(self):
        result = self.result(client_update={"client": "codex", "result": "current"})
        hooks = self.hooks("2026-10-18T14:00:01.000+02:00")
        self.assertTrue(
            gate.timing(result, hooks, self.STDERR, "live").endswith(
                "client ready 1.0 s | preflight 2.4 s | client_update current 3.2 s"
            )
        )

    def test_headless_has_no_launch(self):
        result = {"phases": {"checks": 0.1, "image": 0.2, "start": 3.0, "client": 0.4}}
        self.assertIn("client ready n/a", gate.timing(result, "", "", "live"))

    def test_no_hook_after_launch(self):
        hooks = self.hooks("2026-10-18T13:59:00.000+02:00")
        self.assertIn("client ready n/a", gate.timing(self.result(), hooks, "", "live"))

    def test_stderr_missing(self):
        result = self.result(client_update={"client": "codex", "result": "updated"})
        text = gate.timing(result, "", "", "live")
        self.assertIn("preflight n/a", text)
        self.assertIn("client_update updated n/a", text)


class RefreshedAtTest(unittest.TestCase):
    def test_first_renewal_of_that_container(self):
        lines = [
            line("refresh", G2, token("renewed"), time="2026-10-18T12:01:00Z"),
            line("refresh", G1, token("pending", reason="x"), time="2026-10-18T12:02:00Z"),
            line("refresh", G1, token("renewed"), time="2026-10-18T12:03:00Z"),
        ]
        self.assertEqual(gate.refreshed_at(lines, f"agent-pm-live-{G1}"), "2026-10-18T12:03:00Z")
        self.assertIsNone(gate.refreshed_at(lines, "agent-pm-live-" + "c" * 32))


class NewGenerationTest(unittest.TestCase):
    def test_one_new(self):
        self.assertEqual(gate.new_generation({G1}, {G1, G2}), G2)

    def test_none_or_two(self):
        for after in ({G1}, {G1, G2, "c" * 32}):
            with self.subTest(after=len(after)), self.assertRaises(SystemExit) as raised:
                gate.new_generation({G1}, after, step="restart")
            self.assertIn("restart: want one new generation", str(raised.exception))


class InstallStepsTest(unittest.TestCase):
    PHASES = {"checks": 0.1, "image": 0.5, "start": 4.0, "relay": 0.3, "tmux": 0.2, "client": 1.0}
    BROKER = "".join(
        f"agent-pm-broker: start {p} 0.10 s\n"
        for p in (
            "preflight",
            "image",
            "checks",
            "create",
            "bootstrap",
            "clients",
            "credentials",
            "total",
        )
    )

    def test_clean_claude_and_codex(self):
        self.assertEqual(gate.install_steps({"phases": self.PHASES}, self.BROKER, "claude"), [])
        codex = self.BROKER + "agent-pm-broker: start client_update 2.00 s\n"
        self.assertEqual(gate.install_steps({"phases": self.PHASES}, codex, "codex"), [])

    def test_build_phase(self):
        (problem,) = gate.install_steps({"phases": {**self.PHASES, "build": 9.0}}, "", "claude")
        self.assertIn("build", problem)

    def test_client_update_on_claude(self):
        stderr = self.BROKER + "agent-pm-broker: start client_update 2.00 s\n"
        (problem,) = gate.install_steps({"phases": self.PHASES}, stderr, "claude")
        self.assertIn("client_update", problem)

    def test_unknown_broker_phase(self):
        stderr = self.BROKER + "agent-pm-broker: start install 2.00 s\n"
        (problem,) = gate.install_steps({"phases": self.PHASES}, stderr, "codex")
        self.assertIn("install", problem)


class OpenedDraftTest(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(gate.opened_draft([], is_draft=True))
        self.assertTrue(
            gate.opened_draft(["labeled", "ready_for_review", "convert_to_draft"], is_draft=False)
        )
        self.assertFalse(gate.opened_draft(["convert_to_draft"], is_draft=True))
        self.assertFalse(gate.opened_draft(["labeled"], is_draft=False))


class BotPrTest(unittest.TestCase):
    REFRESHED = "2026-10-18T12:05:00.123456789Z"

    def test_bot_draft_after_refresh(self):
        self.assertEqual(gate.bot_pr(view(), [], self.REFRESHED, need_refresh=True), [])

    def test_human_author(self):
        (problem,) = gate.bot_pr(view(login="someone"), [], self.REFRESHED, need_refresh=True)
        self.assertIn("someone", problem)

    def test_never_draft(self):
        (problem,) = gate.bot_pr(view(draft=False), [], self.REFRESHED, need_refresh=True)
        self.assertIn("draft", problem)

    def test_created_before_refresh(self):
        early = view(created="2026-10-18T12:00:00Z")
        (problem,) = gate.bot_pr(early, [], self.REFRESHED, need_refresh=True)
        self.assertIn("before", problem)

    def test_no_refresh(self):
        (problem,) = gate.bot_pr(view(), [], None, need_refresh=True)
        self.assertIn("no token refresh recorded", problem)

    def test_without_refresh(self):
        self.assertEqual(gate.bot_pr(view(), ["ready_for_review"], None, need_refresh=False), [])


class PendingExceptTest(unittest.TestCase):
    def test_rules(self):
        removed = f"agent-pm-live-{G1}"
        entry = {"container": removed, "account": "ophis", "expires_at": "2026-10-18T13:00:00Z"}
        self.assertEqual(gate.pending_except({"revocations": []}, removed), [])
        self.assertEqual(gate.pending_except({"revocations": [entry]}, removed), [])
        other = {**entry, "container": f"agent-pm-live-{G2}"}
        (problem,) = gate.pending_except({"revocations": [entry, other]}, removed)
        self.assertIn(G2, problem)
        self.assertEqual(len(gate.pending_except({"revocations": [entry]}, None)), 1)


class UnreachableDocTest(unittest.TestCase):
    def test_gate_runs_the_documented_commands(self):
        with open(DOCS, encoding="utf-8") as f:
            text = f.read()
        section = text.split("## What a container cannot reach", 1)[1].split("\n## ", 1)[0]
        documented = {}
        for row in section.splitlines():
            found = re.match(r"\| (.+?) \| `docker exec \"\$C\" sh -c '(.+)'` \|", row)
            if found:
                documented[found[1]] = found[2].replace("\\|", "|")
        self.assertEqual(documented, gate.UNREACHABLE)


class NoSecretTest(unittest.TestCase):
    def test_outputs_never_echo_values(self):
        secret = token("pending", reason="offline", note=FAKE, token=FAKE)
        lines = [line("close", G1, secret, withdrawn()), {**line("refresh", G1), "error": FAKE}]
        outputs = [
            *gate.reclaimed(lines, G1, "claude"),
            gate.removed_tokens(lines, G1),
            *gate.baseline(inspect(HostConfig__CapDrop=[]), "live", CWD, [CLAUDE_DIR]),
            gate.timing({"phases": {}}, "", TimingTest.STDERR, "live"),
            *gate.install_steps({"phases": {}}, TimingTest.STDERR, "claude"),
        ]
        for text in outputs:
            self.assertNotIn(FAKE, text)


if __name__ == "__main__":
    unittest.main()
