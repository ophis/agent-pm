"""Host callbacks bind their saved server and launch generation before marking."""

import os
from unittest import mock

from agent_pm.session.hosts.tmux import hook

from session.tests.case import Case


class CallbackTest(Case):
    def setUp(self):
        self.executable = os.path.join(self.home, "executable")
        with open(self.executable, "w"):
            pass
        os.chmod(self.executable, 0o700)
        self.state = {
            "events": None,
            "sid": "sid",
            "host_state": {
                "executable": self.executable,
                "target": "w1",
                "generation": "generation",
                "environment": {"TMUX_TMPDIR": self.home},
            },
        }

    def run_callback(self, identity):
        response = "" if identity is None else f"%3\t{identity}\n"
        with (
            mock.patch.dict(
                os.environ,
                {"TMUX": "foreign", "TMUX_TMPDIR": "/foreign", "KEEP": "value"},
                clear=True,
            ),
            mock.patch(
                "subprocess.run",
                side_effect=[
                    mock.Mock(returncode=0, stdout=response, stderr=""),
                    mock.Mock(returncode=0, stdout="", stderr=""),
                ],
            ) as run,
        ):
            hook.on_hook("working", {}, self.state)
        return run

    def test_callback_uses_the_saved_selector_for_lookup_and_mark(self):
        run = self.run_callback("sid\tgeneration")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                [
                    self.executable,
                    "display-message",
                    "-p",
                    "-t",
                    "=w1:",
                    "#{pane_id}\t#{@sid}\t#{@generation}",
                ],
                [self.executable, "set-option", "-t", "%3", "@state", "working"],
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], {"TMUX_TMPDIR": self.home, "KEEP": "value"})

    def test_a_reused_name_even_with_the_same_sid_is_never_marked(self):
        for identity in ("sid\tnext-generation", "other\tgeneration", "", None):
            with self.subTest(identity=identity):
                self.assertEqual(self.run_callback(identity).call_count, 1)
