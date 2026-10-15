"""HarnessDelegate names its harness session in a Host's close failure; its harness environment
holds no launcher variable; preview and instruct write no state; owner overlays shape its run;
resume continues a run in the mode it records or in the one it is given, and a run whose process
died, its tui agent session left open, at once.
"""

import json
import os
from unittest import mock

from agent_pm.delegate import ConfigError, Finished, Result, Spec
from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.state import RunState
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Exited, Liveness, Mode, Reply, SessionError
from agent_pm.session import Spec as SessionSpec

from delegate.tests.fakes.harness_session import (
    HarnessSession,
    RecordingHost,
    ShowingHost,
    harness_launches,
)
from delegate.tests.harness import SID, HarnessCase


class CloseTest(HarnessCase):
    def test_a_failed_harness_close_names_the_harness_session(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=self.work)
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
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=self.work)
        record = self.make(spec=spec).detach(outer=outer)
        stored = RecordingHost.processes["agent", self.metadata()["harness_name"]].record()
        self.assertEqual((record.name, stored), ("TASK-1", record))
        self.assertEqual(HarnessDelegate.attach(record).resume(), record)


class ForgetTest(HarnessCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.agent_pm, "delegates")

    def test_forget_deletes_the_runs_state_dir_files_and_keeps_its_workdir(self):
        view = HarnessDelegate.attach(self.make().detach())
        view.close()
        self.assertTrue(os.path.isfile(self.state_record()))
        view.forget()
        self.assertEqual(os.listdir(self.state), [])
        self.assertTrue(os.path.isfile(os.path.join(self.work, "delegate.json")))
        view.forget()
        self.assertEqual(os.listdir(self.state), [])

    def test_a_run_holding_its_run_lock_is_refused_and_keeps_its_files(self):
        view = HarnessDelegate.attach(self.make().detach())
        view.close()
        with RunState(self.work).run_lock(), self.assertRaisesRegex(ConfigError, "running"):
            view.forget()
        self.assertTrue(os.path.isfile(self.state_record()))


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
            RunParams(input="Hello.", out=out, workdir=self.work),
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


class SaveFailureTest(HarnessCase):
    def test_a_failed_result_save_still_hands_on_the_runs_real_finished(self):
        delegate, events, real = self.make(), [], os.replace
        published = os.path.join(self.work, "delegate.json")

        def replace(src, dst):
            if str(dst) == published:
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
        with self.assertRaisesRegex(ConfigError, "invalid delegate.json: 'overlays'"):
            HarnessDelegate.attach(self.work, session_cls=HarnessSession)

    def test_resume_resolves_with_the_overlays_it_was_built_with(self):
        record = self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},)).detach()
        self.stop(record)
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")


class ResumeModeTest(HarnessCase):
    def gave_up(self, mode):
        """A detached run whose harness ended without an outcome, a tui agent session left open."""
        spec = Spec("input", self.work, mode=mode, client="fake", sid=SID, cwd=self.work)
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
        self.assertEqual(agent.state(), Liveness.ABSENT)
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
        self.assertEqual(HarnessSession.live[before["agent_name"]].state(), Liveness.LIVE)


class CrashTest(HarnessCase):
    def crashed(self, cwd=None):
        """A tui run whose process died mid-run: no outcome stored, no run lock held, its agent
        session left live as the run started it.
        """
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=cwd or self.work)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work, client="fake", mode=Mode.TUI, sid=SID, name=state["agent_name"]
        )
        HarnessSession.start(agent, host=state["host"])
        return HarnessDelegate.attach(self.work, session_cls=HarnessSession), state["agent_name"]

    def test_a_tui_run_whose_process_died_is_not_alive(self):
        view, name = self.crashed()
        self.assertEqual(HarnessSession.live[name].state(), Liveness.LIVE)
        self.assertFalse(view.alive())
        self.assertFalse(view.status().alive)

    def test_resume_closes_the_left_agent_session_and_launches_the_next_attempt(self):
        view, name = self.crashed()
        left = HarnessSession.live[name]
        launches = len(harness_launches())
        record = view.resume()
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(left.state(), Liveness.ABSENT)
        self.assertEqual(record.sid, SID)
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
        self.assertEqual(left.state(), Liveness.LIVE)
        self.assertEqual(len(harness_launches()), launches)

    def test_resume_refuses_a_run_replaced_since_its_attach(self):
        view, _name = self.crashed()
        other = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        self.make(spec=Spec("input", self.work, task="echo", sid=other, cwd=self.work))
        launches = len(harness_launches())
        with self.assertRaisesRegex(ConfigError, f"run {SID} was replaced by {other}"):
            view.resume()
        self.assertEqual(
            (len(harness_launches()), self.metadata()["spec"]["sid"]), (launches, other)
        )

    def test_the_status_of_an_agent_session_another_process_started_comes_from_its_plan(self):
        view, _name = self.crashed()
        self.assertEqual(view.session_info(), HarnessSession.history)
        (inspected,) = HarnessSession.inspected
        self.assertEqual((inspected.sid, inspected.mode, inspected.client), (SID, "tui", "fake"))
