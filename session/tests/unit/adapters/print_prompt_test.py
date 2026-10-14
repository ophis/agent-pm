"""PrintPromptAdapter factory and configuration."""

from agent_pm.session.adapters.print_prompt import PrintPromptAdapter
from agent_pm.session.adapters.print_prompt.in_process import PrintPromptInProcess
from agent_pm.session.engine.types import (
    ConfigError,
    SessionStatus,
)

from session.tests.case import Case


class Factory(Case):
    def test_in_process_only_a_fresh_run_each_call(self):
        adapter = PrintPromptAdapter({})
        self.assertEqual((adapter.headless(), adapter.tui()), (None, None))
        first, second = adapter.in_process(), adapter.in_process()
        self.assertIsInstance(first, PrintPromptInProcess)
        self.assertIsNot(first, second)

    def test_no_keys_no_runtime_note_and_the_base_defaults(self):
        adapter = PrintPromptAdapter({})
        self.assertEqual((adapter.keys, adapter.runtime_note), (frozenset(), ""))
        self.assertIsNone(adapter.usage_probe())
        self.assertEqual(adapter.session_status("s", "/w"), SessionStatus("unknown", None, ""))

    def test_a_destination_key_is_a_config_error(self):
        for key in ("roles", "output"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as caught:
                    PrintPromptAdapter({key: {}})
                self.assertEqual(
                    str(caught.exception), f"unknown key {key!r} in PrintPromptAdapter's config"
                )
