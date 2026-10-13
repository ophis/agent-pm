"""session/tests/fakes/session.py: FakeSession plays its script in the caller's process, with no
Host to display on.
"""

import unittest

from agent_pm.session import Displayable, DriverError, NotDisplayable, Usage
from agent_pm.session.engine.types import Exited, Reply, Spec, TurnEnd

from session.tests.case import Case
from session.tests.fakes.host import FakeHost
from session.tests.fakes.session import FakeSession


class Scripted(Case):
    def start(self, cls, name=None, **kw):
        return cls.start("fake", "headless", "the prompt", Spec(workdir=self.home, name=name), **kw)

    def test_events_are_the_script_then_exited_0_and_reply_the_last_one(self):
        session = self.start(FakeSession.scripted(Reply("first"), TurnEnd("second"), TurnEnd()))
        self.assertEqual(
            list(session.events()), [Reply("first"), TurnEnd("second"), TurnEnd(), Exited(0)]
        )
        self.assertEqual(session.reply, "second")
        self.assertEqual(list(session.events()), [])

    def test_a_script_ending_with_exited_keeps_its_returncode(self):
        self.assertEqual(list(self.start(FakeSession.scripted(Exited(3))).events()), [Exited(3)])

    def test_start_records_its_arguments_and_names_the_session_from_the_sid(self):
        cls = FakeSession.scripted()
        session = self.start(cls, resume="abc12345-6789", display="d")
        self.assertEqual((session.sid, session.name), ("abc12345-6789", "fake-abc12345"))
        self.assertEqual(cls.started[0]["prompt"], "the prompt")
        self.assertEqual(cls.started[0]["display"], "d")
        self.assertEqual(session.info().cwd, self.home)
        session.send("more")
        self.assertEqual(session.sent, ["more"])

    def test_a_live_name_is_refused_until_closed(self):
        cls = FakeSession.scripted()
        first = self.start(cls, name="w1")
        with self.assertRaises(DriverError):
            self.start(cls, name="w1")
        first.close()
        self.assertEqual(first.state(), "absent")
        self.assertEqual(self.start(cls, name="w1").state(), "live")

    def test_attach_and_list_reach_only_live_sessions(self):
        cls = FakeSession.scripted()
        one, two = self.start(cls, name="w1"), self.start(cls, name="w2")
        self.assertIs(cls.attach("w1"), one)
        self.assertEqual([row.name for row in cls.list()], ["w1", "w2"])
        self.assertEqual([row.name for row in cls.list("w2")], ["w2"])
        two.stop()
        self.assertEqual([row.name for row in cls.list()], ["w1"])
        for name in ("w2", "nope"):
            with self.subTest(name=name), self.assertRaises(DriverError):
                cls.attach(name)

    def test_it_cannot_display(self):
        session = self.start(FakeSession.scripted())
        self.assertFalse(session.displayable)
        for method in (session.show, session.attach_command, session.screen):
            with self.subTest(method=method.__name__), self.assertRaises(NotDisplayable):
                method()

    def test_class_methods_answer_as_a_session_layer_with_one_fake_host(self):
        cls = FakeSession.scripted()
        self.assertIsNone(cls.usage("fake"))
        self.assertIsNone(cls.layout())
        self.assertEqual(cls.writable_dirs("fake", Spec(workdir=self.home)), ())
        self.assertEqual(cls.display("d"), "d")
        host = cls.host()
        self.assertIsInstance(host, FakeHost)
        self.assertNotIsInstance(host, Displayable)
        self.assertEqual((host.name, cls.host("h").name), ("fake", "h"))

    def test_classes_from_scripted_share_no_state(self):
        first, second = FakeSession.scripted(Reply("a")), FakeSession.scripted()
        self.start(first, name="w1")
        first.configure(adapters={}, hosts={}, default_host="fake")
        first.usage_result = Usage(None, None)
        self.assertEqual((second.started, second.live, second.script), ([], {}, ()))
        self.assertIsNone(second.configured)
        self.assertIsNone(second.usage("fake"))
        self.assertEqual(first.configured["default_host"], "fake")
        self.assertEqual(first.usage("fake"), Usage(None, None))
        self.assertEqual(len(first.started), 1)
        self.start(second, name="w1")

    def test_a_fresh_class_plays_an_empty_script_on_state_no_subclass_shares(self):
        cls = FakeSession.scripted()
        session = self.start(cls, name="w1")
        cls.configure(adapters={}, hosts={}, default_host="fake")
        self.assertEqual(list(session.events()), [Exited(0)])
        self.assertIs(cls.attach("w1"), session)
        self.assertEqual([row.name for row in cls.list()], ["w1"])
        self.assertEqual([call["spec"].name for call in cls.started], ["w1"])
        other = cls.scripted()
        self.assertEqual((other.started, other.live, other.configured), ([], {}, None))
        self.start(other, name="w1")
        self.assertEqual(len(cls.started), 1)


if __name__ == "__main__":
    unittest.main()
