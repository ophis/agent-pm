"""Records behaviour."""

import os
import stat
import sys

from session.tests.unit.tui_hook.case import LINE, PANE, TMUX, TS, WORDS, HookBase, write_state


class RecordsTest(HookBase):
    def test_each_call_appends_one_record_line_ts_label_payload(self):
        payload = {"session_id": "s1", "text": "a\nb\u2028c\U0001f600", "n": [1, None, True, 2.5]}
        self.assertEqual(self.hook("stop", payload), "")
        self.assertEqual(self.hook("tool", {"tool_name": "Bash"}), "")
        records = self.records()
        self.assertEqual([list(r) for r in records], [["ts", "label", "payload"]] * 2)
        self.assertEqual(
            [(r["label"], r["payload"]) for r in records],
            [("stop", payload), ("tool", {"tool_name": "Bash"})],
        )
        for record in records:
            self.assertRegex(record["ts"], TS)
        self.assertEqual(stat.S_IMODE(os.stat(self.hooks).st_mode), 0o600)

    def test_each_word_marks_the_pane_done_and_blocked_with_an_event_line(self):
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                calls = [["set-option", "-t", PANE, "@state", word]]
                if word == "working":
                    self.assertEqual(self.lines(), "")
                else:
                    calls.append(["display-message", "-p", "-t", PANE, "#S"])
                    self.assertRegex(self.lines(), LINE % word)
                self.assertEqual(self.calls(), calls)
        self.assertEqual([r["label"] for r in self.records()], list(WORDS))

    def test_the_tmux_session_json_names_runs_never_one_on_path(self):
        self.assertEqual(self.hook("ask"), "")
        self.assertEqual(
            self.calls(),
            [
                ["set-option", "-t", PANE, "@state", "blocked"],
                ["display-message", "-p", "-t", PANE, "#S"],
            ],
        )
        self.assertRegex(self.lines(), LINE % "blocked")
        self.assertFalse(os.path.exists(self.decoy))

    def test_a_tmux_that_is_no_regular_executable_file_is_one_stderr_line_after_the_record(self):
        plain = os.path.join(self.root, "plain")
        with open(plain, "w") as f:
            f.write(TMUX.format(python=sys.executable))
        os.chmod(plain, 0o644)
        for tmux in (plain, os.path.join(self.root, "bin"), os.path.join(self.root, "missing")):
            with self.subTest(tmux):
                write_state(self.dir, {**self.state, "tmux_path": tmux})
                self.assertError(self.hook("stop"), tmux)
        self.assertEqual([r["label"] for r in self.records()], ["stop"] * 3)
        self.assertEqual((self.calls(), self.lines()), ([], ""))
        self.assertFalse(os.path.exists(self.decoy))

    def test_a_label_without_a_word_is_recorded_and_marks_nothing(self):
        self.assertEqual(self.hook("other", {"x": 1}), "")
        self.assertEqual(
            [(r["label"], r["payload"]) for r in self.records()], [("other", {"x": 1})]
        )
        self.assertEqual(self.calls(), [])

    def test_no_events_file_marks_the_state_with_no_line(self):
        write_state(self.dir, {**self.state, "events": None})
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                self.assertEqual(self.calls(), [["set-option", "-t", PANE, "@state", word]])
        self.assertEqual(self.lines(), "")
        self.assertEqual(len(self.records()), 3)

    def test_no_tmux_pane_records_and_marks_nothing(self):
        for pane in (None, ""):
            self.assertEqual(self.hook("stop", TMUX_PANE=pane), "")
        self.assertEqual(len(self.records()), 2)
        self.assertEqual((self.calls(), self.lines()), ([], ""))

    def test_valid_edge_states_are_accepted(self):
        write_state(
            self.dir,
            {
                **self.state,
                "words": {},
                "project": True,
                "cwd": "",
                "transcript": "/t.jsonl",
                "resume": "abc",
                "later": [1],
            },
        )
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.calls(), [])
