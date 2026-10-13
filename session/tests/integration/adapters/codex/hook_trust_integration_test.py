"""Native hook trust and preflight behavior."""

import concurrent.futures
import contextlib
import dataclasses
import json
import os
import select as select_module
import signal
import subprocess
import sys
import time
from unittest import mock

from agent_pm.session.adapters.codex import tui_start
from agent_pm.session.engine.types import (
    Exited,
    SessionRequest,
    TurnEnd,
)

from session.tests import live_tmux
from session.tests.integration.adapters.codex import fake_tui
from session.tests.integration.adapters.codex.tui_fixtures import (
    WAIT,
    Live,
    Stream,
)


class HookTrustIntegrationTest(Live):
    def test_interleaved_catalog_notifications_exact_hashes_and_cleanup(self):
        self.scene([["trusted"]])
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("trusted"))
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        received = [r["received"] for r in self.logged() if "received" in r]
        self.assertEqual(
            [r["method"] for r in received], ["initialize", "initialized", "hooks/list"]
        )
        self.assertEqual(received[-1]["params"], {"cwds": [str(self.cwd)]})
        self.assertEqual(
            received[0],
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-pm", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        rows = next(r["catalog"] for r in self.logged() if "catalog" in r)
        argv = self.calls()[0]["argv"]
        config = fake_tui.configure([argv[i + 1] for i, word in enumerate(argv) if word == "-c"])
        self.assertEqual(
            config["hooks"]["state"],
            {row["key"]: {"trusted_hash": row["currentHash"]} for row in rows},
        )
        self.assertEqual(
            [row["eventName"] for row in rows],
            [
                "sessionStart",
                "userPromptSubmit",
                "postToolUse",
                "permissionRequest",
                "stop",
                "interrupt",
            ],
        )
        self.assertEqual([row["timeoutSec"] for row in rows], [10, 10, 10, 10, 10, 3])
        self.assertFalse((self.codex_home / "config.toml").exists())

    @staticmethod
    def pid_exists(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        else:
            return True

    def test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive(self):
        cases = (
            "off",
            "disabled",
            "foreign",
            "missing",
            "duplicate",
            "command",
            "hash",
            "malformed",
            "errors",
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                self.scene(catalog_case=case)
                handle = self.start(
                    f"bad{index}", sid=f"bad-{index}", events=str(self.root / f"events-{index}")
                )
                stream = Stream(self, handle)
                self.server.tmux("run-shell", "-b", "true")
                live_tmux.wait(
                    lambda *, index=index: (
                        "codex tui:"
                        in self.server.tmux(
                            "capture-pane", "-p", "-J", "-S", "-1000", "-t", f"=bad{index}:"
                        ).stdout
                    ),
                    WAIT,
                    what="preflight diagnostic",
                )
                self.assertEqual(stream.until(Exited), [Exited(1)])
                self.assertFalse(any(row.get("mode") == "tui" for row in self.logged()))
                self.assertTrue(
                    all(not self.pid_exists(row["pid"]) for row in self.calls("app-server"))
                )
                self.assertIsNone(self.bound(f"bad-{index}").native_id)
                handle.stop()

    def test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed(self):
        for index, status in enumerate(("untrusted", "modified", "trusted", "disabled", "managed")):
            with self.subTest(status=status):
                self.scene(user_hook=status)
                before = len(self.calls())
                handle = self.start(f"u{index}", sid=f"user-{index}")
                stream = Stream(self, handle)
                if status in ("untrusted", "modified"):
                    live_tmux.wait(
                        lambda *, index=index: (
                            "review it"
                            in self.server.tmux(
                                "capture-pane", "-p", "-J", "-S", "-1000", "-t", f"=u{index}:"
                            ).stdout
                        ),
                        WAIT,
                        what="user hook trust diagnostic",
                    )
                    self.assertEqual(stream.until(Exited), [Exited(1)])
                    self.assertEqual(len(self.calls()), before)
                else:
                    self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
                    self.assertEqual(len(self.calls()), before + 1)
                    if status != "disabled":
                        live_tmux.wait(
                            lambda: any(h.get("source") == "user" for h in self.hooks()),
                            WAIT,
                            what="trusted user hook result",
                        )
                        self.assertTrue(
                            all(h["exit"] == 0 for h in self.hooks() if h.get("source") == "user")
                        )
                handle.stop()
        self.assertFalse((self.codex_home / "hooks.json").exists())

    def test_appserver_exit_is_retried_once_then_cleaned_up(self):
        self.scene(catalog_case="exit")
        handle = self.start()
        live_tmux.wait(
            lambda: (
                "app-server exited"
                in self.server.tmux("capture-pane", "-p", "-J", "-S", "-1000", "-t", "=w1:").stdout
            ),
            WAIT,
            what="bounded retry diagnostic",
        )
        self.assertEqual(Stream(self, handle).until(Exited), [Exited(1)])
        probes = self.calls("app-server")
        self.assertEqual(len(probes), 2)
        self.assertTrue(all(not self.pid_exists(row["pid"]) for row in probes))
        self.assertEqual(self.calls(), [])

    def test_concurrent_independent_starts_serialize_the_same_home_probe(self):
        self.scene(catalog_delay=0.1)

        def start(index):
            return self.start(
                f"parallel{index}",
                sid=f"parallel-{index}",
                events=str(self.root / f"parallel-{index}.events"),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            handles = list(pool.map(start, (0, 1)))
        for handle in handles:
            self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        logs = self.logged()
        positions = [i for i, row in enumerate(logs) if row.get("mode") == "app-server"]
        first, second = (logs[i]["pid"] for i in positions)
        closed = next(i for i, row in enumerate(logs) if row.get("probe_closed") == first)
        self.assertLess(positions[0], closed)
        self.assertLess(closed, positions[1])
        self.assertNotEqual(self.bound("parallel-0").native_id, self.bound("parallel-1").native_id)
        self.assertFalse(self.pid_exists(first))
        self.assertFalse(self.pid_exists(second))

    def test_silent_appserver_times_out_and_kills_group_with_a_fake_clock(self):
        self.scene(catalog_case="silent")
        run = self.driver.registry.adapter("codex").tui()
        launch = run.launch(
            "hello",
            SessionRequest(dataclasses.replace(self.spec, sid=self.sid), resumed=False),
            (sys.executable, "-V"),
        )
        data = json.loads(launch.argv[-1])
        env = {**dict(os.environ), **launch.env}
        clock, select, wait = (
            time.monotonic,
            select_module.select,
            subprocess.Popen.wait,
        )
        started = clock()
        with (
            mock.patch.object(
                tui_start.time,
                "monotonic",
                side_effect=lambda: (
                    clock() * 100 if self.log.exists() and self.log.stat().st_size else clock()
                ),
            ),
            mock.patch.object(
                select_module,
                "select",
                side_effect=lambda r, w, e, t: select(r, w, e, min(t, 0.01)),
            ),
            mock.patch.object(
                subprocess.Popen,
                "wait",
                lambda proc, timeout=None: wait(
                    proc, timeout=min(timeout, 0.01) if timeout is not None else None
                ),
            ),
            self.assertRaisesRegex(tui_start.StartError, "no answer"),
        ):
            tui_start.hooks_list(data["argv"][0], data["hook_flags"], str(self.cwd), env)
        self.assertLess(clock() - started, 3)
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        self.assertEqual(self.calls(), [])

    def test_stop_and_sigterm_during_preflight_kill_probe_and_owned_descendants(self):
        def cleanup(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)

        def running(pid):
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

        for index, action in enumerate(("stop", "term")):
            with self.subTest(action=action):
                before = len(self.calls("app-server"))
                descendants_before = len([r for r in self.logged() if "descendant" in r])
                self.scene(catalog_case="silent_tree")
                handle = self.start(f"interrupted{index}", sid=f"interrupted-{index}")
                probe = live_tmux.wait(
                    lambda *, before=before: self.calls("app-server")[before:] or None,
                    WAIT,
                    what="silent preflight",
                )[0]
                self.addCleanup(cleanup, probe["pid"])
                descendants = live_tmux.wait(
                    lambda *, descendants_before=descendants_before: (
                        [r["descendant"] for r in self.logged() if "descendant" in r][
                            descendants_before:
                        ]
                        if len([r for r in self.logged() if "descendant" in r])
                        >= descendants_before + 2
                        else None
                    ),
                    WAIT,
                    what="owned descendants",
                )
                if action == "stop":
                    handle.stop()
                else:
                    os.kill(probe["parent_pid"], signal.SIGTERM)
                live_tmux.wait(
                    lambda *, descendants=descendants, probe=probe: (
                        not self.pid_exists(probe["pid"])
                        and all(not running(pid) for pid in descendants)
                    ),
                    4,
                    what="interrupted preflight cleanup",
                )
                self.assertEqual(len(self.calls("app-server")), before + 1)
                self.assertEqual(self.calls(), [])
