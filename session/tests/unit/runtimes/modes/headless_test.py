"""Headless and JsonHeadless contract tests."""

from agent_pm.session.engine.types import Launch, Reply, Spec
from agent_pm.session.runtimes.modes.headless import Headless, JsonHeadless

from session.tests.case import Case
from session.tests.modes_support import StubHeadless


class StreamHeadless(Headless):
    """Reads its client's lines itself: no parse."""

    def launch(self, spec: Spec):
        return Launch(("stub", spec.prompt))

    def events(self, lines):
        for line in lines:
            yield Reply(line.strip())


class HeadlessTest(Case):
    def test_the_stream_is_required_and_parse_is_not(self):
        self.assertEqual(list(StreamHeadless().events(["a\n", "b"])), [Reply("a"), Reply("b")])
        for interface, method in ((Headless, "events"), (JsonHeadless, "parse")):
            launch_only = type("LaunchOnly", (interface,), {"launch": StubHeadless.launch})
            with self.subTest(missing=method), self.assertRaisesRegex(TypeError, method):
                launch_only()

    def test_events_parses_each_json_object_line_in_order_and_skips_the_rest(self):
        lines = [
            '{"texts": ["a"]}\n',
            "not json\n",
            "[1, 2]\n",
            "\n",
            "",
            '"text"\n',
            "null\n",
            "[" * 100000 + "]" * 100000 + "\n",
            "{}\n",
            '{"texts": ["b", "c"]}',
        ]
        self.assertEqual(list(StubHeadless().events(lines)), [Reply("a"), Reply("b"), Reply("c")])
