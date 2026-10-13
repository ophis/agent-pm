"""Driver tui behavior with caller-owned dependencies."""

import dataclasses
import fcntl
import json
import os
import stat
from unittest import mock

from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    DriverError,
    IdleTimeout,
    SessionInfo,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests import fake_adapter
from session.tests.unit.driver.support import (
    BAD_NAMES,
    LAUNCHED,
    PYTHONPATH,
    SID,
    TMUX,
    TuiBase,
)


class TuiStartTest(TuiBase):
    def test_driver_binds_the_adapter_session_selector_for_new_and_resumed_sessions(self):
        self.configure(tables={"claude": {"implementation": "adapters.claude:ClaudeAdapter"}})
        for resumed, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resumed=resumed):
                self.tui(
                    runtime="claude",
                    name=f"w{resumed}",
                    **({"resume": SID} if resumed else {"sid": SID}),
                )
                argv = self.host.start.call_args.args[1].argv
                self.assertEqual(tuple(argv[:3]), ("claude", word, SID))

    def test_effective_project_override_reaches_info_record_and_tui_state(self):
        original = fake_adapter.FakeTui.launch
        for project in (True, False):

            def launch(run, *args, project=project):
                return dataclasses.replace(original(run, *args), project=project)

            with (
                self.subTest(project=project),
                mock.patch.object(fake_adapter.FakeTui, "launch", launch),
            ):
                handle = self.tui(
                    name=f"project-{project}", spec=self.spec(access=Access(project=not project))
                )
            self.assertIs(handle.info().project, project)
            self.assertIs(records.session_record(self.workdir, handle.sid)["project"], project)
            self.assertIs(tui.read_state(handle.name)["project"], project)

    def test_prepares_the_session_dir_records_the_session_then_begins_it_tagged_with_no_pane(self):
        order = []

        def status(_name):
            order.append(("status", os.path.exists(records.path(self.workdir))))

        self.host.status.side_effect = status
        self.host.start.side_effect = lambda *_a, **_k: order.append(
            ("start", [e["kind"] for e in self.written()])
        )
        handle = self.tui(name="w1", sid=SID, events=self.events)
        self.assertEqual(order, [("status", False), ("start", ["session"])])
        self.assertEqual(
            [c[0] for c in self.host.mock_calls],
            ["events_file", "tmux_path", "status", "start", "pane"],
        )
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode), ("w1", SID, "fake", "tui")
        )
        self.assertEqual(
            handle.info(),
            SessionInfo(
                sid=SID, native_id="", cwd=self.workdir, project=False, transcript="", resume=""
            ),
        )
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.args[0], "w1")
        self.assertEqual(
            call.args[1].argv[-5:], ["hello", *tui.hook_argv(os.path.join(self.root, "w1"))]
        )
        self.assertEqual(call.args[1].cwd, self.workdir)
        self.assertEqual(
            call.kwargs,
            {
                "options": tmux_host.StartOptions(
                    events=self.events, status_line=False, tags={"@sid": SID, "@runtime": "fake"}
                )
            },
        )
        self.assertEqual(call.args[1].env["PWD"], self.workdir)
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertEqual(
            {
                k: v
                for k, v in tui.read_state("w1").items()
                if k in ("runtime", "sid", "events", "words", "tmux_path")
            },
            {
                "runtime": "fake",
                "sid": SID,
                "events": self.events,
                "words": {"stop": "done", "ask": "blocked", "tool": "working"},
                "tmux_path": TMUX,
            },
        )

    def test_no_tmux_on_path_is_a_driver_error_with_nothing_started(self):
        self.host.tmux_path.return_value = None
        self.refused(DriverError, "tmux not found", lambda: self.tui(name="w1"))
        self.assertEqual(self.host.mock_calls, [mock.call.tmux_path()])
        self.assertFalse(os.path.lexists(self.root))

    def test_the_names_lock_is_held_from_prepare_to_begin(self):
        held = []

        def locked(*_args, **_kwargs):
            fd = os.open(os.path.join(self.root, "w1.lock"), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)

        self.host.status.side_effect = self.host.start.side_effect = locked
        self.tui(name="w1")
        locked()
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError):
            self.tui(name="w1")
        locked()
        self.assertEqual(held, [True, True, False, True, False])

    def test_the_default_name_is_the_runtime_and_the_sids_first_8_chars(self):
        for kwargs in ({"sid": SID}, {"resume": SID}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.tui(**kwargs).name, "fake-0f8fad5b")
                self.assertEqual(self.host.start.call_args.args[0], "fake-0f8fad5b")
        handle = self.tui()
        self.assertEqual(handle.name, f"fake-{handle.sid[:8]}")

    def test_a_bad_name_given_or_made_from_the_sid_is_refused_before_tmux_or_a_path(self):
        self.name_refused(lambda name: self.tui(name=name), BAD_NAMES[:-1])
        self.refused(ConfigError, "name must be None or a string", lambda: self.tui(name=5))
        with self.assertRaises(ConfigError) as caught:
            self.tui(sid="a b")
        self.assertEqual(str(caught.exception), "name must match [A-Za-z0-9_-]+, got 'fake-a b'")
        self.nothing_hosted()
        self.assertFalse(os.path.exists(records.path(self.workdir)))

    def test_a_bad_events_file_is_refused(self):
        os.mkdir(self.events)
        sharp = os.path.join(self.dir, "a#b")
        for bad, message in (
            (self.events, f"events file {self.events}: Is a directory"),
            (sharp, f"events file {sharp}: tmux would misread it"),
            (5, "events must be a printable path, got 5"),
            ("/a\nb", r"events must be a printable path, got '/a\nb'"),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError, message, lambda *, bad=bad: self.tui(name="w1", events=bad)
                )
        self.host.status.assert_not_called()
        self.assertFalse(os.path.lexists(self.root))

    def test_a_running_or_unknown_session_is_refused_with_no_record(self):
        for status, message in (
            (tmux_host.RUNNING, "tui session w1 is running; tmux attach -t '=w1'"),
            (tmux_host.UNKNOWN, "tui session w1: its tmux state is unknown"),
        ):
            with self.subTest(status=status):
                self.host.status.return_value = status
                self.refused(DriverError, message, lambda: self.tui(name="w1"))
        self.host.start.assert_not_called()

    def test_a_failed_tmux_start_is_a_driver_error_after_the_record_removing_the_session_dir(self):
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            self.tui(name="w1")
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertEqual([e["kind"] for e in self.written()], ["session"])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_the_status_line_is_the_tui_settings(self):
        self.configure(tui=True, settings={"status_line": True})
        self.tui(name="w1")
        self.assertIs(self.host.start.call_args.kwargs["options"].status_line, expr2=True)

    def test_a_bad_tui_setting_is_refused_before_anything(self):
        self.configure(tui=True, settings={"status_line": "yes"})
        self.refused(ConfigError, "status_line: want true or false", lambda: self.tui(name="w1"))
        self.nothing_hosted()

    def test_the_handles_state_screen_send_and_stop_are_the_tui_modes(self):
        handle = self.tui(name="w1")
        self.host.status.return_value = tmux_host.RUNNING
        self.host.read.return_value = "pane text"
        self.assertEqual((handle.state(), handle.screen()), ("live", "pane text"))
        handle.send("hi")
        self.host.paste.assert_called_once_with("%1", "hi")
        self.host.keys.assert_called_once_with("%1", "Enter")
        handle.stop()
        self.host.kill.assert_called_once_with("%1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))


class EnvironmentTest(TuiBase):
    def setUp(self):
        super().setUp()
        LAUNCHED.clear()

    def environ(self, **values):
        patch = mock.patch.dict(os.environ, {"HOME": os.environ["HOME"], **values}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def env(self, name, **fields) -> dict[str, str]:
        self.tui(runtime="inner", name=name, spec=self.spec(**fields))
        env = dict(self.host.start.call_args.args[1].env)
        self.assertEqual((env.pop("HOME"), env.pop("PWD")), (os.environ["HOME"], self.workdir))
        return env

    def test_spec_name_is_the_session_name_and_protect_gains_the_hook_paths(self):
        self.tui(runtime="inner", name="w1", spec=self.spec(protect=("/c",)), events=self.events)
        self.tui(runtime="inner", sid=SID)
        first, second = LAUNCHED
        self.assertEqual((first.name, first.protect), ("w1", ("/c", TMUX, self.events, self.root)))
        self.assertEqual(
            (second.name, second.protect, second.sid), ("inner-0f8fad5b", (TMUX, self.root), SID)
        )

    def test_every_session_drops_adapter_identities_and_merges_session_and_launch_env(self):
        self.environ(INNER_ID="i", OTHER_SESSION="", OTHER_ID="o", LAUNCH_DROP="x", KEEP="k")
        self.assertEqual(self.env("w1"), {"KEEP": "k", "LAUNCH_SET": "1"})
        flags = ("--custom", "v")
        self.assertEqual(
            self.env(
                "w2", flags=flags, env={"PAGER": "less", "OTHER_MODE": "off", "LAUNCH_SET": "spec"}
            ),
            {"KEEP": "k", "LAUNCH_SET": "1", "PAGER": "less", "OTHER_MODE": "off"},
        )
        self.assertEqual(LAUNCHED[-1].flags, flags)

    def test_a_child_default_is_dropped_only_inside_its_runtime_and_at_its_value(self):
        self.environ(PAGER="cat", NO_COLOR="1", OTHER_MODE="on")
        both = {"PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on", "LAUNCH_SET": "1"}
        self.assertEqual(self.env("w1"), both)
        self.environ(INNER_SESSION="1", PAGER="cat", NO_COLOR="0", OTHER_MODE="on")
        self.assertEqual(self.env("w2"), {"NO_COLOR": "0", "OTHER_MODE": "on", "LAUNCH_SET": "1"})
        self.environ(OTHER_SESSION="1", PAGER="cat", NO_COLOR="1", OTHER_MODE="on")
        self.assertEqual(self.env("w3"), {"PAGER": "cat", "NO_COLOR": "1", "LAUNCH_SET": "1"})
        self.assertEqual(self.env("w4", env={"OTHER_MODE": "on"}), both)

    def test_codex_injected_defaults_drop_only_inside_codex_at_their_injected_value(self):
        self.configure(tables={"codex": {"implementation": "adapters.codex:CodexAdapter"}})
        inside = {"CODEX_THREAD_ID": "t"}
        for environ, dropped in (
            ({"CLAUDECODE": "1", "NO_COLOR": "1", "PAGER": "cat"}, set()),
            ({**inside, "NO_COLOR": "0", "PAGER": "less", "CODEX_CI": "x"}, set(inside)),
            (
                {**inside, "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "CODEX_CI": "1"},
                {*inside, "NO_COLOR", "PAGER", "GIT_PAGER", "CODEX_CI"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                child = self.env("codex-defaults")
                self.assertEqual(set(environ) - set(child), dropped)

    def test_session_env_keys_need_no_adapter_allowlist(self):
        self.environ(KEEP="k")
        self.assertEqual(
            self.env("w1", env={"EDITOR": "vi", "ZED": "1"}),
            {"KEEP": "k", "LAUNCH_SET": "1", "EDITOR": "vi", "ZED": "1"},
        )

    def test_an_adapter_failing_to_load_refuses_every_start(self):
        self.configure(
            tui=True, tables={"broken": {"implementation": "adapters.broken:BrokenAdapter"}}
        )
        self.refused(
            ConfigError,
            "adapter 'broken': agent_pm.session.adapters.broken:BrokenAdapter: cannot import "
            "agent_pm.session.adapters.broken: No module named 'agent_pm.session.adapters.broken'",
            lambda: self.tui(runtime="inner", name="w1"),
        )
        self.host.start.assert_not_called()


class SessionEnvTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.configure("env", probe={"argv": ("probe-cmd",)})
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": "/parent"}))
        self.access = Access(commands=("git",), env={"PYTHONPATH": PYTHONPATH})

    def test_a_headless_session_gets_it_and_its_usage_probe_does_not(self):
        handle, _ = self.run_once(runtime="env", spec=self.spec(access=self.access))
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        self.assertEqual(entry["env"]["PYTHONPATH"], PYTHONPATH)
        with mock.patch.object(self.driver, "probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), Usage(Window(7.0, None), None))
        self.assertEqual(probe.call_args.kwargs["env"]["PYTHONPATH"], "/parent")

    def test_a_tui_session_gets_it(self):
        self.tui(runtime="env", name="w1", spec=self.spec(access=self.access))
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.args[1].env["PYTHONPATH"], PYTHONPATH)


class AttachTest(TuiBase):
    def started(self, *hooked) -> None:
        """Tui session w1 started (sid SID) on pane %1, then a hooks.jsonl record per (label,
        payload) in `hooked`, its @sid SID; the host's calls cleared.
        """
        self.tui(name="w1", sid=SID, events=self.events)
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "w") as f:
            f.writelines(
                json.dumps({"ts": "", "label": label, "payload": payload}) + "\n"
                for label, payload in hooked
            )
        self.host.option.side_effect = lambda target, key: (
            SID if (target, key) == ("%1", "@sid") else None
        )
        self.host.reset_mock()

    def test_a_handle_of_the_session_with_its_reply_native_id_and_next_records_events(self):
        self.started(("tool", {"session_id": "n1"}), ("stop", {"last_message": "r1"}))
        handle = self.driver.attach("w1", idle_timeout=2)
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode, handle.reply),
            ("w1", SID, "fake", "tui", "r1"),
        )
        self.assertEqual(
            handle.info(),
            SessionInfo(
                sid=SID, native_id="n1", cwd=self.workdir, project=False, transcript="", resume=""
            ),
        )
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.pane("w1"),
                mock.call.option("%1", "@sid"),
                mock.call.option("%1", "@state"),
            ],
        )
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            f.write(
                json.dumps({"ts": "", "label": "stop", "payload": {"last_message": "r2"}}) + "\n"
            )
        self.assertEqual(next(handle.events()), TurnEnd("r2", 0))
        self.assertEqual(handle.reply, "r2")

    def test_attach_refreshes_bound_metadata_without_starting_a_session(self):
        self.started(("tool", {"session_id": "n1"}))
        records.note(records.path(self.workdir), "result", outcome={"status": "done"})

        def metadata(_adapter, info):
            self.assertEqual(info.native_id, "n1")
            return dataclasses.replace(info, transcript="/attached", resume="resume n1")

        with mock.patch.object(fake_adapter.FakeAdapter, "session_metadata", metadata):
            handle = self.driver.attach("w1")
        self.assertEqual(handle.info().transcript, "/attached")
        self.assertEqual(records.session_record(self.workdir, SID)["resume"], "resume n1")
        self.assertEqual(sum(e["kind"] == "session" for e in self.written()), 1)

    def test_attached_while_blocked_no_idle_timeout_until_its_next_event(self):
        """On a fake clock (time.monotonic, time.sleep), a tool record comes at 10 s."""
        hooks = os.path.join(self.root, "w1", "hooks.jsonl")
        for hooked, state, first in (
            ((("tool", {}), ("ask", {})), None, (Activity(""), 10)),
            ((), "blocked", (Activity(""), 10)),
            ((("ask", {}), ("tool", {})), None, (IdleTimeout(1), 1)),
            ((("ask", {}), ("stop", {})), "working", (IdleTimeout(1), 1)),
        ):
            with self.subTest(hooked=hooked, state=state):
                self.host.status.return_value = None
                self.started(*hooked)
                options = {"@sid": SID, "@state": state}
                self.host.option.side_effect = lambda _target, key, *, options=options: options[key]
                self.host.status.return_value, self.host.read.return_value = tmux_host.RUNNING, ""
                now = [0.0]

                failure = AssertionError("no event")

                def sleep(seconds, *, now=now, failure=failure):
                    if now[0] > 60:
                        raise failure
                    now[0] += seconds
                    if now[0] - seconds < 10 <= now[0]:
                        with open(hooks, "a") as f:
                            f.write(json.dumps({"ts": "", "label": "tool", "payload": {}}) + "\n")

                with (
                    mock.patch("time.monotonic", lambda *, now=now: now[0]),
                    mock.patch("time.sleep", sleep),
                ):
                    event = next(self.driver.attach("w1", idle_timeout=1).events())
                self.assertEqual((event, round(now[0])), first)

    def test_a_bad_name_or_idle_timeout_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.driver.attach)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.driver.attach("w1", idle_timeout=bad)
            self.assertEqual(
                str(caught.exception),
                f"idle_timeout must be a positive number of seconds, got {bad!r}",
            )
        self.nothing_hosted()

    def test_no_session_dir_or_another_sid_is_no_tui_session(self):
        with self.assertRaises(DriverError) as caught:
            self.driver.attach("w1")
        self.assertTrue(str(caught.exception).startswith("no tui session w1: "), caught.exception)
        self.nothing_hosted()
        self.started()
        for found in ("other", None):
            with self.subTest(found=found), self.assertRaises(DriverError) as caught:
                self.host.option.side_effect = lambda _name, _key, *, found=found: found
                self.driver.attach("w1")
            self.assertEqual(str(caught.exception), "no tui session w1")

    def test_a_runtime_without_tui_or_a_registry_error_is_a_driver_error(self):
        self.started()
        self.configure(tui=False)
        for message in (
            "no tui session w1: runtime 'fake' has no tui mode",
            "no tui session w1: no [adapters.fake] table in the configured adapters",
        ):
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                self.driver.attach("w1")
            self.assertEqual(str(caught.exception), message)
            self.configure_session({})
        self.host.option.assert_not_called()
