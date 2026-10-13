"""Payload behaviour."""

import io
import os

from agent_pm.session.modes import tui_hook

from session.tests.unit.tui_hook.case import HookBase, write_state


class PayloadTest(HookBase):
    def test_strs_over_4096_chars_are_cut_unless_the_word_is_done(self):
        long = "\u00e9" * 5000
        key = "k" * 5000
        payload = {"a": long, "b": [{"c": "y" * 4097}, "z" * 4096], "n": 5, key: "v"}
        cut = {"a": long[:4096], "b": [{"c": "y" * 4096}, "z" * 4096], "n": 5, key: "v"}
        for label in ("tool", "ask", "other", "stop"):
            self.assertEqual(self.hook(label, payload), "")
        self.assertEqual([r["payload"] for r in self.records()], [cut, cut, cut, payload])

    def test_bad_stdin_is_an_empty_payload_and_a_stderr_line(self):
        cases = (
            b"",
            b"not json",
            b"[1]",
            b'"text"',
            b"\xff\xfe\xfd",
            b"[" * 100000,
        )
        for data in cases:
            with self.subTest(data[:20]):
                self.assertError(self.hook("tool", data), "payload")
        self.assertEqual([r["payload"] for r in self.records()], [{}] * len(cases))

    def test_a_payload_of_64_mib_is_read(self):
        data = b'{"a": "' + b"x" * ((64 << 20) - 9) + b'"}'
        self.assertEqual(len(data), 64 << 20)
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write(
                'def on_hook(label, payload, state):\n    payload["length"] = len(payload["a"])\n'
            )
        self.state["adapter_hook"] = callback
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", data), "")
        self.assertEqual(
            [r["payload"] for r in self.records()], [{"a": "x" * 4096, "length": (64 << 20) - 9}]
        )

    def test_overflow_drains_stdin_and_writes_no_record(self):
        stream = io.BytesIO(b"x" * (tui_hook.PAYLOAD_MAX + 1))
        with self.assertRaisesRegex(tui_hook.HookError, "payload: over"):
            tui_hook.main([self.dir, "stop"], stream)
        self.assertEqual(stream.read(), b"")
        self.assertNothingWritten()

    def test_adapter_callback_receives_full_payload_before_truncation_and_can_skip(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write(
                'def on_hook(label, payload, state):\n    payload["original_length"] = '
                'len(payload.get("text", ""))\n    return label != '
                '"ask"\n'
            )
        self.state["adapter_hook"] = callback
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", {"text": "x" * 5000}), "")
        record = self.records()[0]
        self.assertEqual(record["payload"], {"text": "x" * 4096, "original_length": 5000})
        marked = self.calls()
        self.assertEqual(self.hook("ask", {"text": "skip"}), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.calls(), marked)

    def test_callback_file_requires_absolute_regular_owned_module_with_on_hook(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write("value = 1\n")
        for path in ("relative.py", os.path.join(self.root, "absent.py"), self.root, callback):
            with self.subTest(path=path):
                self.state["adapter_hook"] = path
                write_state(self.dir, self.state)
                self.assertError(self.hook("stop"), "adapter")
        self.assertNothingWritten()
