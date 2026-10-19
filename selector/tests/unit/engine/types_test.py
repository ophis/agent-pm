"""selector/engine/types.py and base.py: the Selector interface, its state, question, config,
backends and errors.
"""

import dataclasses
import inspect
import unittest

from agent_pm.selector import (
    Answer,
    Backend,
    Failure,
    Kind,
    Question,
    Selector,
    SelectorConfig,
    SelectorError,
    State,
)

from utils.tests.case import Case


class Interface(Case):
    def test_select_is_the_only_abstract_method(self):
        self.assertEqual(Selector.__abstractmethods__, frozenset({"select"}))
        self.assertEqual(
            list(inspect.signature(Selector.select).parameters), ["self", "state", "question"]
        )

    def test_select_returns_an_answer(self):
        self.assertIs(inspect.signature(Selector.select).return_annotation, Answer)
        self.assertEqual([f.name for f in dataclasses.fields(Answer)], ["pick", "confidence"])
        answer = Answer("high", 0.8)
        self.assertEqual((answer.pick, answer.confidence), ("high", 0.8))
        self.assertIsNone(Answer("only", None).confidence)
        field = "pick"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(answer, field, "low")

    def test_a_selector_without_select_cannot_be_built(self):
        with self.assertRaises(TypeError):
            type("Half", (Selector,), {})()


class Values(Case):
    def test_the_state_sent_is_its_fields_then_the_kept_droppable_items(self):
        state = State({"title": "t", "status": "s"}, ("comments", ("old", "mid", "new")))
        self.assertEqual(
            state.sent(), {"title": "t", "status": "s", "comments": ["old", "mid", "new"]}
        )
        self.assertEqual(list(state.sent()), ["title", "status", "comments"])
        self.assertEqual(state.sent(1)["comments"], ["mid", "new"])
        self.assertEqual(state.sent(3)["comments"], [])
        self.assertEqual(State({"b": 1, "a": 2}).sent(), {"b": 1, "a": 2})
        self.assertEqual(list(State({"b": 1, "a": 2}).sent()), ["b", "a"])

    def test_a_state_and_a_question_hold_frozen_copies(self):
        fields, options, items = {"title": "t"}, {"a": "desc a"}, ["c1"]
        state = State(fields, ("comments", items))
        question = Question("Pick.", options)
        fields["title"], options["b"] = "changed", "desc b"
        items.append("c2")
        self.assertEqual(state.sent(), {"title": "t", "comments": ["c1"]})
        self.assertEqual(dict(question.options), {"a": "desc a"})
        self.assertEqual(state.droppable, ("comments", ("c1",)))
        for mapping in (state.fields, question.options):
            with self.subTest(mapping=mapping), self.assertRaises(TypeError):
                mapping["x"] = "y"
        field = "fields"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(state, field, {})

    def test_the_question_keeps_its_options_order(self):
        question = Question("Pick.", {"b": "desc b", "a": "desc a"})
        self.assertEqual(list(question.options.items()), [("b", "desc b"), ("a", "desc a")])

    def test_a_score_question_needs_two_to_ten_levels(self):
        self.assertEqual([k.value for k in Kind], ["choice", "score"])
        self.assertIs(Question("Pick.", {"a": "desc a"}).kind, Kind.CHOICE)
        levels = {f"l{i}": f"level {i}" for i in range(11)}
        for count in (2, 10):
            with self.subTest(count=count):
                options = dict(list(levels.items())[:count])
                question = Question("Score.", options, Kind.SCORE)
                self.assertIs(question.kind, Kind.SCORE)
                self.assertEqual(list(question.options), list(options))
        for count in (1, 11):
            with self.subTest(count=count), self.assertRaises(ValueError):
                Question("Score.", dict(list(levels.items())[:count]), Kind.SCORE)
        self.assertEqual(len(Question("Pick.", levels).options), 11)
        with self.assertRaises(ValueError):
            Question("Pick.", {"a": "", "b": ""}, "rank")

    def test_bad_questions_or_states_are_refused_when_built(self):
        for instructions, options in (
            ("Pick.", {}),
            ("Pick.", {"a": "desc a", " ": "blank"}),
            ("Pick.", {"a": "desc a", "": "empty"}),
            (" ", {"a": "desc a"}),
            ("", {"a": "desc a"}),
        ):
            with (
                self.subTest(instructions=instructions, options=options),
                self.assertRaises(ValueError),
            ):
                Question(instructions, options)
        for fields, droppable in (
            ({"title": "t"}, (" ", ())),
            ({"title": "t"}, ("", ("c",))),
            ({"title": "t", "comments": []}, ("comments", ("c",))),
            ({"title": object()}, None),
            ({"title": "t"}, ("comments", (object(),))),
            ({"title": float("nan")}, None),
        ):
            with self.subTest(fields=fields, droppable=droppable), self.assertRaises(ValueError):
                State(fields, droppable)


class Config(Case):
    def test_config_is_frozen_with_no_key_command_by_default(self):
        config = SelectorConfig(
            Backend.KEV, "http://127.0.0.1:8000/v1/systemone", "kev-1.0", 2.0, 32000
        )
        self.assertEqual((config.window, config.key_command), (32000, ()))
        field = "url"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(config, field, "x")

    def test_the_window_comes_before_the_key_command(self):
        names = [f.name for f in dataclasses.fields(SelectorConfig)]
        self.assertEqual(names, ["backend", "url", "model", "timeout", "window", "key_command"])

    def test_repr_hides_the_key_command(self):
        config = SelectorConfig(
            Backend.JEV,
            "https://api.example/v1/systemone",
            "jev-1.13.0",
            2.0,
            32000,
            ("/usr/bin/x", "secret-name"),
        )
        self.assertNotIn("secret-name", repr(config))
        self.assertNotIn("/usr/bin/x", repr(config))


class Codes(Case):
    def test_backends_and_failures_are_exactly_these(self):
        self.assertEqual([b.value for b in Backend], ["jev", "kev"])
        self.assertEqual(
            [f.value for f in Failure], ["error", "timeout", "no-key", "invalid", "too-large"]
        )

    def test_an_error_carries_only_its_code(self):
        error = SelectorError(Failure.TIMEOUT)
        self.assertEqual(error.reason, Failure.TIMEOUT)
        self.assertEqual(str(error), "timeout")


if __name__ == "__main__":
    unittest.main()
