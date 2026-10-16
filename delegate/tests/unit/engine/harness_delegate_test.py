"""HarnessDelegate names its harness session in a Host's close failure; its harness environment
holds no launcher variable; a run's state lives only in its run dir, which counts attempts, and a
refused start creates nothing; preview and instruct write no state; owner overlays shape its run;
resume continues a run in the mode it records or in the one it is given, and a run whose process
died, its tui agent session left open, at once; replace, resume and detach first kill the callback
group a dead harness left; checkouts lists its agent dir's real checkouts and clean deletes that
dir, refusing a live run or a dir out of place.
"""

import fcntl
import json
import os
import shutil
import signal
from unittest import mock

from agent_pm.delegate import ConfigError, Finished, Result, Spec
from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.engine import harness_delegate
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.state import RunState
from agent_pm.delegate.engine.types import Layout
from agent_pm.session import Access, Exited, Liveness, Mode, NotDisplayable, Reply, SessionError
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Spec as SessionSpec

from delegate.tests.fakes.harness_session import (
    HarnessSession,
    RecordingHost,
    ShowingHost,
    harness_launches,
)
from delegate.tests.harness import SID, HarnessCase
from delegate.tests.unit.engine.orphans import Clock, Orphan

ORPHAN = 4242


class CloseTest(HarnessCase):
    def test_a_failed_harness_close_names_the_harness_session(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec, host="agent", harness_host="harness").detach()
        name = self.metadata()["harness_name"]
        process = RecordingHost.processes["harness", name]
        with (
            mock.patch.object(process, "close", side_effect=SessionError("tmux: boom")),
            self.assertRaises(SessionError) as caught,
        ):
            HarnessDelegate.attach(record).close()
        self.assertEqual(str(caught.exception), f"close {name}: tmux: boom")


class OuterTest(HarnessCase):
    def test_the_hosts_record_is_the_one_detach_returns_named_by_the_outer_starter(self):
        outer = {
            "kind": "pipeline",
            "name": "TASK-1",
            "ctl": ("agent-pm-linear",),
            "resume": ("agent-pm-linear", "resume", "TASK-1"),
        }
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach(outer=outer)
        stored = RecordingHost.processes["agent", self.metadata()["harness_name"]].record()
        self.assertEqual((record.name, stored), ("TASK-1", record))
        self.assertEqual(HarnessDelegate.attach(record).resume(), record)


class RunDirTest(HarnessCase):
    def record(self):
        return os.path.join(self.work, "run", "delegate.json")

    def stored(self):
        with open(self.record(), encoding="utf-8") as file:
            return json.load(file)

    def corrupt(self, text):
        self.make()
        with open(self.record(), "w", encoding="utf-8") as file:
            file.write(text)

    def test_the_run_record_is_the_runs_only_state(self):
        self.make()
        self.assertEqual(sorted(os.listdir(self.work)), ["agent", "run"])
        self.assertEqual(self.stored()["spec"]["sid"], SID)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.work, "run"))), ["delegate.json", "delegate.lock"]
        )
        self.assertEqual(os.listdir(self.agent_pm), ["config.local.toml"])

    def test_refused_start_creates_nothing(self):
        top = os.path.join(self.home, "a")
        self.work = os.path.join(top, "b", "work")
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(len(HarnessSession.preflights), 1)
        HarnessSession.preflight_error = None
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            self.make(role="absent-role")
        self.assertFalse(os.path.lexists(top))
        self.assertEqual(os.listdir(self.agent_pm), ["config.local.toml"])

    def test_a_refused_start_on_a_workdir_makes_none_of_its_dirs(self):
        os.mkdir(self.work)
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(os.listdir(self.work), [])

    def test_a_live_run_refuses_a_new_one_before_making_the_agent_dir(self):
        self.make()
        os.rmdir(os.path.join(self.work, "agent"))
        before = self.stored()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.assertEqual((os.listdir(self.work), self.stored()), (["run"], before))

    def test_a_new_run_of_the_same_sid_counts_the_next_attempt(self):
        self.make()
        self.make()
        self.assertEqual(self.stored()["attempt"], 2)
        self.make(spec=Spec("input", self.work, client="fake", sid=SID.replace("0", "1")))
        self.assertEqual(self.stored()["attempt"], 1)

    def test_a_corrupt_run_record_keeps_a_readable_attempt(self):
        for text, attempt in (("not json", 1), ('{"attempt": 4}', 5)):
            with self.subTest(text=text):
                self.corrupt(text)
                self.make()
                self.assertEqual(self.stored()["attempt"], attempt)

    def test_a_held_run_lock_keeps_a_corrupt_run_record(self):
        self.corrupt("not json")
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        with open(self.record(), encoding="utf-8") as file:
            self.assertEqual(file.read(), "not json")

    def test_an_old_layout_workdir_names_its_missing_run_record(self):
        self.make()
        os.replace(self.record(), os.path.join(self.work, "delegate.json"))
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {self.record()}: missing metadata")


def tree(top):
    found = {}
    for root, _dirs, files in os.walk(top):
        for name in files:
            with open(os.path.join(root, name), "rb") as file:
                found[os.path.relpath(os.path.join(root, name), top)] = file.read()
    return found


class CleanTest(HarnessCase):
    def agent(self):
        return Layout(self.work).agent

    def refused(self, message, workdir=None):
        for dry in (False, True):
            with self.subTest(dry=dry), self.assertRaises(ConfigError) as caught:
                HarnessDelegate.clean(workdir or self.work, dry=dry)
            self.assertEqual(str(caught.exception), message)

    def test_checkouts_lists_real_src_entries(self):
        self.assertIsNone(HarnessDelegate.checkouts(self.work))
        self.make()
        self.assertEqual(HarnessDelegate.checkouts(self.work), ())
        src = os.path.join(self.agent(), "src")
        real = os.path.join(os.path.realpath(self.work), "agent", "src")
        for path in ("o/r-TASK-1", "p/q-TASK-1"):
            os.makedirs(os.path.join(src, path))
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "n-TASK-1"))
        os.symlink(os.path.join(elsewhere, "n-TASK-1"), os.path.join(src, "o", "s-TASK-1"))
        os.symlink(elsewhere, os.path.join(src, "x"))
        with open(os.path.join(src, "o", "f-TASK-1"), "w") as file:
            file.write("x")
        self.assertEqual(
            HarnessDelegate.checkouts(self.work),
            (os.path.join(real, "o", "r-TASK-1"), os.path.join(real, "p", "q-TASK-1")),
        )
        moved = os.path.join(self.home, "moved")
        os.replace(self.agent(), moved)
        os.symlink(moved, self.agent())
        self.assertEqual(HarnessDelegate.checkouts(self.work), ())

    def test_clean_deletes_agent_files_and_keeps_run_records(self):
        self.make()
        deep = os.path.join(self.agent(), "tmp", "deep")
        os.makedirs(deep)
        os.makedirs(os.path.join(self.agent(), "src", "o", "r-TASK-1"))
        with open(os.path.join(deep, "pr.md"), "w") as file:
            file.write("x")
        os.chmod(os.path.join(deep, "pr.md"), 0o444)
        with open(os.path.join(self.work, "notes.md"), "w") as file:
            file.write("kept")
        run = tree(Layout(self.work).run)
        name = HarnessDelegate.attach(self.work).status().name
        HarnessDelegate.clean(self.work, dry=True)
        self.assertTrue(os.path.isdir(deep))
        self.assertIsNone(HarnessDelegate.clean(self.work))
        self.assertFalse(os.path.lexists(self.agent()))
        self.assertEqual(tree(Layout(self.work).run), run)
        self.assertEqual(sorted(os.listdir(self.work)), ["notes.md", "run"])
        self.assertIsNone(HarnessDelegate.checkouts(self.work))
        self.assertEqual(HarnessDelegate.attach(self.work).status().name, name)
        self.assertIsNone(HarnessDelegate.clean(self.work))
        never = os.path.join(self.home, "never")
        self.assertIsNone(HarnessDelegate.clean(never))
        self.assertFalse(os.path.lexists(never))

    def test_clean_refuses_a_live_run(self):
        self.make()
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        with open(Layout(self.work).run + "/delegate.json", "w") as file:
            file.write("not json")
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_without_agent_files_refuses_a_live_run(self):
        self.make()
        shutil.rmtree(self.agent())
        with RunState(Layout(self.work)).run_lock():
            files = tree(self.work)
            self.refused(f"{self.work}: delegate is still alive")
            self.assertEqual(tree(self.work), files)

    def test_clean_without_agent_files_keeps_an_ended_run(self):
        self.make()
        shutil.rmtree(self.agent())
        files = tree(self.work)
        for dry in (False, True):
            with self.subTest(dry=dry):
                self.assertIsNone(HarnessDelegate.clean(self.work, dry=dry))
                self.assertEqual(tree(self.work), files)

    def test_clean_refuses_a_live_detached_run(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec, host="agent", harness_host="harness").detach()
        self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_refuses_an_agent_dir_out_of_place(self):
        self.make()
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "keep"))
        os.replace(self.agent(), os.path.join(self.home, "agent"))
        os.symlink(elsewhere, self.agent())
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "keep")))
        os.unlink(self.agent())
        with open(self.agent(), "w") as file:
            file.write("x")
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        os.unlink(self.agent())
        os.replace(os.path.join(self.home, "agent"), self.agent())
        link = os.path.join(self.home, "link")
        os.symlink(self.work, link)
        self.refused(f"not a real directory: {link}/agent, refusing to touch", link)
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_holds_the_run_mutex_while_it_deletes(self):
        self.make()
        held = []

        def rmtree(path):
            with open(Layout(self.work).run + "/delegate.lock", "rb") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(path)

        with mock.patch.object(harness_delegate.shutil, "rmtree", side_effect=rmtree):
            HarnessDelegate.clean(self.work)
        self.assertEqual(held, [self.agent()])

    def test_a_new_run_after_clean_lays_out_again_with_the_next_attempt(self):
        self.make()
        HarnessDelegate.clean(self.work)
        self.make()
        self.assertTrue(os.path.isdir(self.agent()))
        self.assertEqual(self.metadata()["attempt"], 2)


class PreviewTest(HarnessCase):
    def test_preview_names_the_sessions_hosts_and_callback_and_writes_nothing(self):
        for mode, agent in (("tui", f"dummy-tester-{SID[:8]}"), ("headless", None)):
            with self.subTest(mode=mode):
                spec = Spec("input", self.work, task="echo", mode=mode, client="fake", sid=SID)
                preview = HarnessDelegate.preview(
                    "dummy-tester",
                    spec,
                    session_cls=HarnessSession,
                    harness_host="harness",
                    callback=["writeback", "--issue", "ticket-id"],
                )
                self.assertEqual(
                    (
                        preview.agent,
                        preview.harness,
                        preview.host,
                        preview.harness_host,
                        preview.callback,
                        preview.plan.sid,
                        preview.plan.spec.mode,
                    ),
                    (
                        agent,
                        f"dummy-tester-{SID[:8]}-drive",
                        "agent",
                        "harness",
                        ("writeback", "--issue", "ticket-id"),
                        SID,
                        mode,
                    ),
                )
                self.assertTrue(preview.plan.prompt.endswith("\n# Input\n\ninput\n"))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual(os.listdir(self.agent_pm), ["config.local.toml"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_preview_refuses_an_unknown_role_like_the_constructor(self):
        spec = Spec("input", self.work, client="fake", sid=SID)
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            HarnessDelegate.preview("absent-role", spec, session_cls=HarnessSession)
        self.assertFalse(os.path.exists(self.work))


class InstructTest(HarnessCase):
    def test_instruct_delivers_the_composed_prompt_without_state_or_session(self):
        out = os.path.join(self.work, "prompt.md")
        result = HarnessDelegate.instruct(
            "dummy-tester", Spec("Hello.", self.work, task="echo", out=out)
        )
        self.assertEqual(result, Result(0, None, instruction=out))
        composer = Composer()
        want = composer.render(
            composer.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=Layout(self.work).agent),
        )
        with open(out, encoding="utf-8") as file:
            self.assertEqual(file.read(), want)
        self.assertIn("**Your task**: `echo`", want)
        self.assertEqual(os.listdir(self.work), ["prompt.md"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))


class CheckTuiTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(displayable=True)
        self.settings(harness_host="harness")
        self.events = os.path.join(self.home, "events")

    def check(self, **kw):
        HarnessDelegate.check_tui(session_cls=HarnessSession, **kw)

    def test_the_agents_host_places_the_display_and_the_harness_takes_the_events(self):
        with (
            mock.patch.object(ShowingHost, "layout", autospec=True) as layout,
            mock.patch.object(ShowingHost, "events_path", autospec=True) as events_path,
        ):
            self.check(display="opener/right", events=self.events)
        [(host,)] = [call.args for call in layout.call_args_list]
        self.assertEqual(
            (host.name, layout.call_args.kwargs), ("agent", {"display": "opener/right"})
        )
        [(host, path)] = [call.args for call in events_path.call_args_list]
        self.assertEqual((host.name, path), ("harness", self.events))
        self.assertFalse(os.path.exists(self.events))

    def test_a_host_refusing_the_display_or_the_events_is_a_config_error(self):
        for method, error in (
            ("layout", SessionError("no pane to show the TUI beside (not in tmux)")),
            ("layout", SessionConfigError("no tmux session absent")),
            ("layout", SessionConfigError("display 'a/sideways': not <opener>[/<split>]")),
            ("events_path", SessionConfigError(f"events file {self.events}: Permission denied")),
        ):
            with (
                self.subTest(method=method, error=error),
                mock.patch.object(ShowingHost, method, side_effect=error),
                self.assertRaises(ConfigError) as caught,
            ):
                self.check(display="d", events=self.events)
            self.assertEqual(str(caught.exception), str(error))
        self.assertEqual(RecordingHost.launches, [])


class LaunchTest(HarnessCase):
    def test_the_launchers_variables_stay_out_of_the_harness_environment(self):
        launcher = {"AGENT_PM_SID": SID, "AGENT_PM_EVENTS": "/manager/events", "KEPT": "kept"}
        lease = ("AGENT_PM_MANAGER", "AGENT_PM_HOLDER_ID", "AGENT_PM_HOLDER_GENERATION")
        with mock.patch.dict(os.environ, launcher | dict.fromkeys(lease, "1")):
            self.make().detach()
        environment = RecordingHost.launches[0]["env"]
        for name in ("AGENT_PM_SID", "AGENT_PM_EVENTS", *lease):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["KEPT"], "kept")

    def test_the_harness_is_no_agent_session_so_carries_no_marker(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AGENT_PM_AGENT_SESSION", None)
            self.make().detach()
        self.assertNotIn("AGENT_PM_AGENT_SESSION", RecordingHost.launches[0]["env"])


class SaveFailureTest(HarnessCase):
    def test_a_failed_result_save_still_hands_on_the_runs_real_finished(self):
        delegate, events, real = self.make(), [], os.replace
        record = os.path.join(Layout(self.work).run, "delegate.json")

        def replace(src, dst):
            if str(dst) == record:
                raise OSError("disk full")
            real(src, dst)

        with mock.patch.object(os, "replace", replace):
            result = delegate.run(on_event=events.append)
        finished = [e for e in events if isinstance(e, Finished)]
        self.assertEqual([e.result for e in finished], [result])
        self.assertEqual(result.outcome.status, "done")


class UsageSession(HarnessSession):
    clients = []

    @classmethod
    def usage(cls, client):
        cls.clients.append(client)


class OverlayTest(HarnessCase):
    def test_usage_reads_the_client_the_owner_overlays_resolve(self):
        UsageSession.clients = []
        overlays = ({"roles": {"dummy-tester": {"client": "claude"}}},)
        HarnessDelegate.usage("dummy-tester", session_cls=UsageSession)
        HarnessDelegate.usage("dummy-tester", overlays=overlays, session_cls=UsageSession)
        self.assertEqual(UsageSession.clients, ["fake", "claude"])

    def test_owner_overlays_shape_the_persisted_run(self):
        self.make()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "low")
        self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},))
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")

    def test_a_record_without_overlays_is_corrupt(self):
        self.make()
        path = self.state_record()
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        self.assertEqual(data["overlays"], [])
        del data["overlays"]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file)
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {path}: 'overlays'")

    def test_resume_resolves_with_the_overlays_it_was_built_with(self):
        record = self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},)).detach()
        self.stop(record)
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")


class ResumeModeTest(HarnessCase):
    def gave_up(self, mode):
        """A detached run whose harness ended without an outcome, a tui agent session left open."""
        spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        self.assertFalse(view.alive())

    def resumed(self, **mode):
        launches = len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        record = view.resume(**mode)
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(view.resume(**mode), record)
        self.assertEqual(len(harness_launches()), launches + 1)
        return record, self.metadata()

    def modes(self, state):
        return (state["spec"]["mode"], state["plan"]["session"]["mode"], state["attempt"])

    def test_a_tui_run_that_gave_up_resumes_headless_with_its_sid_and_records_the_mode(self):
        self.gave_up("tui")
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        record, state = self.resumed(mode="headless")
        self.assertEqual((record.sid, record.sessions), (SID, (state["harness_name"],)))
        self.assertEqual(self.modes(state), ("headless", "headless", 2))
        self.assertEqual((state["spec"]["name"], state["spec"]["events"]), (None, None))
        self.assertEqual(agent.alive(), False)
        self.assertEqual(HarnessSession.preflights[-1]["spec"].mode, "headless")

    def test_without_a_mode_a_tui_run_resumes_in_tui(self):
        self.gave_up("tui")
        before = self.metadata()
        record, state = self.resumed()
        self.assertEqual(record.sessions, (before["harness_name"], before["agent_name"]))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]),
            (before["spec"]["name"], before["spec"]["events"]),
        )

    def test_a_headless_run_resumes_in_tui_with_its_agent_session_live(self):
        self.gave_up("headless")
        record, state = self.resumed(mode="tui")
        self.assertEqual(record.sessions, (state["harness_name"], state["agent_name"]))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]), (state["agent_name"], state["events"])
        )
        self.assertEqual(HarnessSession.live[state["agent_name"]].sid, SID)

    def test_a_mode_detach_refuses_is_refused_changing_nothing(self):
        self.gave_up("tui")
        before = self.metadata()
        launches = len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        for mode in ("in_process", "remote"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigError, repr(mode)):
                view.resume(mode=mode)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(HarnessSession.live[before["agent_name"]].alive(), True)


class StatusTest(HarnessCase):
    def test_its_state_is_working_while_the_run_lives_else_gone(self):
        record = self.make().detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual((view.status().state, view.status().hidden), ("working", False))
        self.stop(record)
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))


class ViewTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(displayable=True)
        self.settings(harness_host="harness")

    def test_hide_closes_the_agents_and_the_harness_views_show_opened(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        names = [self.metadata()[key] for key in ("agent_name", "harness_name")]
        view.show()
        self.assertEqual([name for name, _, _ in ShowingHost.shown], names)
        self.assertFalse(view.status().hidden)
        self.assertIsNone(view.hide())
        self.assertEqual(ShowingHost.hidden, names)
        self.assertTrue(view.status().hidden)
        view.close()
        with self.assertRaisesRegex(NotDisplayable, "delegate has no live displayable session"):
            view.hide()
        self.assertEqual(ShowingHost.hidden, names)


class CrashTest(HarnessCase):
    def crashed(self, cwd=None):
        """A tui run whose process died mid-run: no outcome stored, no run lock held, its agent
        session left live as the run started it.
        """
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=cwd)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return HarnessDelegate.attach(self.work, session_cls=HarnessSession), state["agent_name"]

    def test_a_tui_run_whose_process_died_is_not_alive(self):
        view, name = self.crashed()
        self.assertEqual(HarnessSession.live[name].alive(), True)
        self.assertFalse(view.alive())
        self.assertFalse(view.status().alive)

    def test_its_status_is_unknown_while_its_agent_sessions_host_cannot_tell(self):
        view, name = self.crashed()
        agent = HarnessSession.live[name]
        self.assertEqual((view.status().state, view.status().hidden), ("gone", False))
        agent._state = Liveness.UNKNOWN
        self.assertEqual(view.status().state, "unknown")

    def test_resume_closes_the_left_agent_session_and_launches_the_next_attempt(self):
        view, name = self.crashed()
        left = HarnessSession.live[name]
        launches = len(harness_launches())
        record = view.resume()
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(left.alive(), False)
        self.assertEqual(record.sid, SID)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_after_its_agent_dir_was_pruned_makes_it_again(self):
        view, _name = self.crashed()
        shutil.rmtree(Layout(self.work).agent)
        view.resume()
        self.assertTrue(os.path.isdir(Layout(self.work).agent))
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_refused_for_an_untrusted_cwd_leaves_the_agent_session_live(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        self.settings(trusted_dirs=(project,))
        _view, name = self.crashed(cwd=project)
        self.settings()
        left, launches = HarnessSession.live[name], len(harness_launches())
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no longer in trusted_dirs"):
            view.resume()
        self.assertEqual(left.alive(), True)
        self.assertEqual(len(harness_launches()), launches)

    def test_a_resume_its_preflight_refuses_leaves_the_agent_session_live(self):
        _view, name = self.crashed()
        left, launches = HarnessSession.live[name], len(harness_launches())
        HarnessSession.preflight_error = SessionConfigError("fake: no client binary")
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no client binary"):
            view.resume()
        self.assertEqual((left.alive(), len(harness_launches())), (True, launches))

    def test_resume_preflights_the_live_agents_name_under_a_probe_name(self):
        view, name = self.crashed()
        view.resume()
        self.assertNotEqual(HarnessSession.preflights[-1]["spec"].name, name)
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)

    def test_resume_refuses_a_run_replaced_since_its_attach(self):
        view, _name = self.crashed()
        other = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        self.make(spec=Spec("input", self.work, task="echo", sid=other))
        launches = len(harness_launches())
        with self.assertRaisesRegex(ConfigError, f"run {SID} was replaced by {other}"):
            view.resume()
        self.assertEqual(
            (len(harness_launches()), self.metadata()["spec"]["sid"]), (launches, other)
        )

    def test_the_status_of_an_agent_session_another_process_started_comes_from_its_plan(self):
        view, _name = self.crashed()
        self.assertEqual(view.session_status(), HarnessSession.history)
        (inspected,) = HarnessSession.inspected
        self.assertEqual((inspected.sid, inspected.mode, inspected.client), (SID, "tui", "fake"))


class SweepTest(HarnessCase):
    """A callback group left by a dead harness (orphans.Orphan) is killed before anything starts."""

    def setUp(self):
        super().setUp()
        Clock(self)
        self.killpg = self.enterContext(mock.patch("os.killpg"))
        self.seen = []

    def orphan(self) -> Orphan:
        orphan = Orphan(self, RunState(Layout(self.work)).callback_lock, f"{ORPHAN}\n")

        def killpg(pid, signum):
            if signum == signal.SIGKILL:
                self.seen.append((self.metadata()["attempt"], len(harness_launches())))
            orphan.killpg(pid, signum)

        self.killpg.side_effect = killpg
        return orphan

    def record(self) -> bytes:
        with open(self.state_record(), "rb") as file:
            return file.read()

    def kills(self, orphan) -> list:
        return [call for call in orphan.calls if call[1] == signal.SIGKILL]

    def test_a_new_run_kills_the_left_group_before_its_record_replaces_the_old(self):
        self.make()
        orphan = self.orphan()
        self.make()
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_resume_kills_the_left_group_before_it_launches_a_harness(self):
        view = self.crashed()
        orphan = self.orphan()
        launches = len(harness_launches())
        view.resume()
        self.assertEqual(self.seen, [(1, launches)])
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_detach_kills_the_left_group_before_it_launches_a_harness(self):
        delegate = self.make()
        orphan = self.orphan()
        delegate.detach()
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(len(harness_launches()), 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_a_group_that_will_not_end_refuses_each_start_changing_nothing(self):
        view = self.crashed()
        orphan = self.orphan()
        orphan.dies = False
        before, launches = self.record(), len(harness_launches())
        left = HarnessSession.live[self.metadata()["agent_name"]]
        for start in (view.resume, HarnessDelegate.attach(self.work).detach, self.make):
            with self.subTest(start=start), self.assertRaisesRegex(ConfigError, "still exists"):
                start()
            self.assertEqual((self.record(), len(harness_launches())), (before, launches))
            if start == view.resume:
                self.assertEqual(left.alive(), True)

    def test_a_held_run_lock_refuses_a_new_run_though_its_record_says_completed(self):
        self.make().run()
        self.assertTrue(self.metadata()["completed"])
        self.orphan()
        before = self.record()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.killpg.assert_not_called()
        self.assertEqual(self.record(), before)

    def crashed(self) -> HarnessDelegate:
        """A tui run whose harness died, its agent session left live."""
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return HarnessDelegate.attach(self.work, session_cls=HarnessSession)
