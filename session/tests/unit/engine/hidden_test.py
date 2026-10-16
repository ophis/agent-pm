import json
import os
from types import SimpleNamespace

from agent_pm.session.engine import hidden

from session.tests.case import Case


def process(name="w1", sid="s1"):
    return SimpleNamespace(name=name, session_identity=lambda: (sid, "g1"))


class HiddenStore(Case):
    def path(self, name="w1"):
        return os.path.join(self.agent_pm, "hidden", f"{name}.json")

    def test_hide_then_show_rewrites_the_sessions_file(self):
        hidden.record(process(), hidden=True)
        self.assertTrue(hidden.hidden(process()))
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"sid": "s1", "hidden": True})
        hidden.record(process(), hidden=False)
        self.assertFalse(hidden.hidden(process()))
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"sid": "s1", "hidden": False})

    def test_another_sid_or_no_file_is_not_hidden(self):
        self.assertFalse(hidden.hidden(process()))
        hidden.record(process(), hidden=True)
        self.assertFalse(hidden.hidden(process(sid="s2")))
        self.assertFalse(hidden.hidden(process(name="w2")))

    def test_a_session_without_a_sid_records_nothing(self):
        hidden.record(process(sid=None), hidden=True)
        self.assertFalse(os.path.exists(os.path.dirname(self.path())))
        self.assertFalse(hidden.hidden(process(sid=None)))

    def test_a_malformed_file_is_not_hidden(self):
        os.makedirs(os.path.dirname(self.path()))
        for text in ("{", "[]", '{"sid": "s1", "hidden": "yes"}', '{"sid": "s1"}'):
            with self.subTest(text=text):
                with open(self.path(), "w", encoding="utf-8") as f:
                    f.write(text)
                self.assertFalse(hidden.hidden(process()))
