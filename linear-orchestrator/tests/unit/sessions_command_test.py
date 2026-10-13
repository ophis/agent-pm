from agent_pm.linear_orchestrator.linear import sessions
from agent_pm.session import ConfigError, SessionStatus
from orchestrator_test_helpers.unit.sessions.command_fixtures import Command as CommandBase
from orchestrator_test_helpers.unit.sessions.data import CMD, RUNTIME, SID, T1, WORKDIR, text


class Command1(CommandBase):
    def test_the_drivers_resume_command_for_the_session_cwd(self):
        for entry, cwd in (
            (None, WORKDIR),
            ({"kind": "session", "cwd": "/data/my repo", "resume": "rm -rf ~"}, "/data/my repo"),
        ):
            with self.subTest(entry=entry):
                self.session.return_value = entry
                self.info.reset_mock()
                self.assertEqual(sessions.command(self.record()), CMD)
                self.session.assert_called_with(WORKDIR, SID)
                self.info.assert_called_once_with(RUNTIME, SID, cwd)

    def test_built_fresh_each_time_never_read_from_the_record(self):
        rec = self.record()
        rec["command"] = "rm -rf ~"
        self.assertEqual(sessions.body(rec), text(f"running · #1 · {T1}"))
        self.info.return_value = SessionStatus("yes", None, "resume it")
        self.assertEqual(
            sessions.body(rec), f"Run {SID} · running · #2 · {T1}\n\n```\nresume it\n```"
        )
        self.assertEqual(self.info.call_count, 2)

    def test_no_command_block(self):
        for name, effect in (
            ("raises", ConfigError("bad sid")),
            ("unknown", SessionStatus("unknown", None, CMD)),
            ("empty", SessionStatus("yes", None, "")),
        ):
            with self.subTest(name):
                self.info.side_effect = effect if isinstance(effect, Exception) else None
                self.info.return_value = effect
                rec = self.record()
                self.assertIsNone(sessions.command(rec))
                self.assertEqual(sessions.body(rec), f"Run {SID} · running · #1 · {T1}")
