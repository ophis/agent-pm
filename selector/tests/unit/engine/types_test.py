"""selector/engine/types.py and base.py: the Selector interface, its config, backends and errors."""

import dataclasses
import inspect
import unittest

from agent_pm.selector import Backend, Failure, Selector, SelectorConfig, SelectorError

from utils.tests.case import Case


class Interface(Case):
    def test_select_is_the_only_abstract_method(self):
        self.assertEqual(Selector.__abstractmethods__, frozenset({"select"}))
        self.assertEqual(
            list(inspect.signature(Selector.select).parameters), ["self", "state", "choices"]
        )

    def test_a_selector_without_select_cannot_be_built(self):
        with self.assertRaises(TypeError):
            type("Half", (Selector,), {})()


class Config(Case):
    def test_config_is_frozen_with_no_key_command_by_default(self):
        config = SelectorConfig(Backend.KEV, "http://127.0.0.1:8000/v1/systemone", "kev-1.0", 2.0)
        self.assertEqual(config.key_command, ())
        field = "url"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(config, field, "x")

    def test_repr_hides_the_key_command(self):
        config = SelectorConfig(
            Backend.JEV,
            "https://api.example/v1/systemone",
            "jev-1.13.0",
            2.0,
            ("/usr/bin/x", "secret-name"),
        )
        self.assertNotIn("secret-name", repr(config))
        self.assertNotIn("/usr/bin/x", repr(config))


class Codes(Case):
    def test_backends_and_failures_are_exactly_these(self):
        self.assertEqual([b.value for b in Backend], ["jev", "kev"])
        self.assertEqual([f.value for f in Failure], ["error", "timeout", "no-key", "invalid"])

    def test_an_error_carries_only_its_code(self):
        error = SelectorError(Failure.TIMEOUT)
        self.assertEqual(error.reason, Failure.TIMEOUT)
        self.assertEqual(str(error), "timeout")


if __name__ == "__main__":
    unittest.main()
