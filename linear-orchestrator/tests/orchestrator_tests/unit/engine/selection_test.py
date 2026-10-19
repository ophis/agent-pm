"""engine/selection.py: translate over a fake Delegate, the chain's Context and Resolver, and the
ModelResolver and EffortResolver over session's merged client config, their score questions and
confidence floor; FakeResolver passes the resolver contracts.
"""

import dataclasses
import inspect
import json
import os
import unittest
from unittest import mock

import agent_pm.session as session
from agent_pm import utils
from agent_pm.delegate import HarnessDelegate, Outcome
from agent_pm.linear_orchestrator.engine.selection import (
    EFFORT_INSTRUCTIONS,
    EFFORT_INSTRUCTIONS_NO_MODEL,
    MODEL_INSTRUCTIONS,
    Choice,
    Context,
    EffortChoice,
    EffortResolver,
    ModelChoice,
    ModelResolver,
    Resolver,
    SelectionError,
    SelectionFailure,
    translate,
)
from agent_pm.selector import (
    Answer,
    Backend,
    Failure,
    JevSelector,
    Kind,
    SelectorConfig,
    SelectorError,
)
from agent_pm.selector import State as SelectorState
from orchestrator_tests.case import Case
from orchestrator_tests.selection_fixtures import (
    EFFORTS,
    STATE,
    TRANSLATION,
    EffortChoiceContract,
    FakeResolver,
    ModelChoiceContract,
    ResolverContract,
    deliverable,
    state_of,
)

from delegate.tests.fakes.delegate import FakeDelegate
from selector.tests.fakes.selector import FakeSelector

ORCHESTRATOR_CONFIG = (("agent_pm.linear_orchestrator", "config.toml"),)
CLAUDE = Context("engineer", "build", "claude")
OPENCODE = Context("engineer", "build", "opencode")


def session_clients() -> dict:
    """Session's own merged parse of its config (the reference the layers must match)."""
    return utils.read_resources(session.CONFIG_RESOURCES)["clients"]


def provider(outcome=None, *, error=None, run_error=None):
    """A FakeDelegate class whose runs end in outcome; error is raised by create, run_error by run;
    each create is recorded.
    """

    class Translator(FakeDelegate):
        ROLES = {"choice-translator": {"translate": "Translates an issue into a state"}}
        outcome_result = outcome
        created = []

        @classmethod
        def create(cls, role, spec, *, display=None, callback=None, overlays=()):
            cls.created.append((role, spec, overlays))
            if error is not None:
                raise error
            return super().create(role, spec, display=display, callback=callback, overlays=overlays)

        def run(self, on_event=None):
            if run_error is not None:
                raise run_error
            return super().run(on_event)

    return Translator


def done(deliverable):
    return Outcome("done", "Translated", "translated the issue", deliverable=deliverable)


class Translating(Case):
    def workdir(self):
        path = os.path.join(self.home, "translate")
        os.makedirs(path, exist_ok=True)
        return path

    def test_one_headless_run_of_the_role_whose_deliverable_is_the_state(self):
        fake = provider(done(f"  {deliverable()} \n"))
        core = {"output": "x"}
        workdir = self.workdir()
        self.assertEqual(translate("TASK-1", workdir, provider=fake, overlays=(core,)), STATE)
        ((role, spec, overlays),) = fake.created
        self.assertEqual(role, "choice-translator")
        self.assertEqual(
            (spec.input, spec.workdir, spec.task, spec.client, spec.mode, spec.resume),
            ("TASK-1", workdir, "translate", None, "headless", False),
        )
        self.assertEqual(overlays, (core,))

    def test_the_roles_own_config_and_the_board_overlay_run_it_on_haiku_reading_its_ticket(self):
        core = utils.read_resources(ORCHESTRATOR_CONFIG)["core"]
        run = HarnessDelegate.resolve("choice-translator", "translate", overlays=(core,))
        self.assertEqual(
            (list(run.commands), run.client, run.tier, run.effort, run.output["type"]),
            (["agent-pm-linear issue read *"], "claude", 4, "low", "orchestrator"),
        )
        self.assertEqual(session_clients()["claude"]["tiers"][str(run.tier)], "haiku")

    def test_the_real_roles_outcome_is_its_state_or_a_translate_error(self):
        index = HarnessDelegate.roles()
        self.assertEqual(list(index["choice-translator"]), ["translate"])
        for outcome, want in (
            (Outcome("done", "state of TASK-1", "a docs tweak", deliverable=deliverable()), STATE),
            (Outcome("failed", "state of TASK-1", "the ticket read failed"), None),
        ):

            class Role(FakeDelegate):
                ROLES = index
                outcome_result = outcome

            with self.subTest(status=outcome.status):
                if want is not None:
                    self.assertEqual(translate("TASK-1", self.workdir(), provider=Role), want)
                    continue
                with self.assertRaises(SelectionError) as caught:
                    translate("TASK-1", self.workdir(), provider=Role)
                self.assertEqual(caught.exception.reason, SelectionFailure.TRANSLATE)

    def test_the_json_deliverable_becomes_the_state(self):
        comments = [
            {"author": "B", "created_at": "2026-10-19T11:00:00Z", "body": "Newest."},
            {"author": "", "created_at": "2026-10-19T09:00:00Z", "body": "Oldest, first."},
            {"author": "C", "created_at": "2026-10-19T10:00:00Z", "body": "Middle."},
            {"author": "D", "created_at": "2026-10-19T09:00:00Z", "body": "Oldest, second."},
        ]
        translation = {
            "title": "Fix the login page typo",
            "description": "",
            "status": "In Progress",
            "labels": ["Bug", "UI"],
            "comments": comments,
        }
        state = translate(
            "TASK-1", self.workdir(), provider=provider(done(deliverable(translation)))
        )
        self.assertEqual(
            dict(state.fields),
            {
                "title": "Fix the login page typo",
                "description": "",
                "status": "In Progress",
                "labels": ["Bug", "UI"],
            },
        )
        self.assertEqual(state.droppable, ("comments", tuple(comments[i] for i in (1, 3, 2, 0))))
        self.assertEqual(
            translate("TASK-1", self.workdir(), provider=provider(done(deliverable()))),
            state_of(TRANSLATION),
        )

    def test_a_deliverable_off_the_shape_is_invalid(self):
        comment = TRANSLATION["comments"][0]
        cases = {
            "not json": "title: Fix a typo",
            "json and more": deliverable() + " {}",
            "a list": json.dumps([TRANSLATION]),
            "a string": json.dumps("Fix a typo"),
            "a missing key": deliverable({k: v for k, v in TRANSLATION.items() if k != "status"}),
            "an extra key": deliverable({**TRANSLATION, "url": "u"}),
            "a number title": deliverable({**TRANSLATION, "title": 1}),
            "a null description": deliverable({**TRANSLATION, "description": None}),
            "a label list status": deliverable({**TRANSLATION, "status": ["Todo"]}),
            "labels a string": deliverable({**TRANSLATION, "labels": "Bug"}),
            "a number label": deliverable({**TRANSLATION, "labels": ["Bug", 2]}),
            "comments an object": deliverable({**TRANSLATION, "comments": comment}),
            "a string comment": deliverable({**TRANSLATION, "comments": ["Also check"]}),
            "a comment missing a key": deliverable(
                {**TRANSLATION, "comments": [{"author": "A", "body": "x"}]}
            ),
            "a comment with an extra key": deliverable(
                {**TRANSLATION, "comments": [{**comment, "id": "c1"}]}
            ),
            "a null author": deliverable(
                {**TRANSLATION, "comments": [{**comment, "author": None}]}
            ),
            "an author object": deliverable(
                {**TRANSLATION, "comments": [{**comment, "author": {"name": "A"}}]}
            ),
            "a number created_at": deliverable(
                {**TRANSLATION, "comments": [{**comment, "created_at": 1}]}
            ),
            "a null body": deliverable({**TRANSLATION, "comments": [{**comment, "body": None}]}),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(SelectionError) as caught:
                translate("TASK-1", self.workdir(), provider=provider(done(text)))
            self.assertEqual(
                (caught.exception.reason, str(caught.exception)),
                (SelectionFailure.INVALID, "invalid"),
            )
            error = caught.exception
            self.assertTrue(error.__context__ is None or error.__suppress_context__)

    def test_a_blank_issue_is_refused_before_any_run(self):
        fake = provider(done("x"))
        with self.assertRaises(ValueError):
            translate(" ", self.workdir(), provider=fake)
        self.assertEqual(fake.created, [])

    def test_a_failed_create_or_run_is_a_translate_error_without_its_text(self):
        for fake in (
            provider(error=RuntimeError("secret issue text")),
            provider(done("x"), run_error=RuntimeError("secret issue text")),
        ):
            with self.subTest(fake=fake), self.assertRaises(SelectionError) as caught:
                translate("TASK-1", self.workdir(), provider=fake)
            self.assertEqual(
                (caught.exception.reason, str(caught.exception)),
                (SelectionFailure.TRANSLATE, "translate"),
            )
            self.assertTrue(caught.exception.__suppress_context__)

    def test_no_outcome_or_one_not_done_is_a_translate_error(self):
        for outcome in (None, Outcome("failed", "No", "could not read the issue")):
            with self.subTest(outcome=outcome), self.assertRaises(SelectionError) as caught:
                translate("TASK-1", self.workdir(), provider=provider(outcome))
            self.assertEqual(
                (caught.exception.reason, str(caught.exception)),
                (SelectionFailure.TRANSLATE, "translate"),
            )

    def test_a_blank_deliverable_is_invalid(self):
        with self.assertRaises(SelectionError) as caught:
            translate("TASK-1", self.workdir(), provider=provider(done(" \n")))
        self.assertEqual(
            (caught.exception.reason, str(caught.exception)), (SelectionFailure.INVALID, "invalid")
        )


class Types(Case):
    def test_choose_and_fallback_are_the_resolvers_abstract_methods(self):
        self.assertEqual(Resolver.__abstractmethods__, frozenset({"choose", "fallback"}))
        self.assertEqual(
            list(inspect.signature(Resolver.choose).parameters), ["self", "state", "context"]
        )
        self.assertEqual(list(inspect.signature(Resolver.fallback).parameters), ["self", "context"])

    def test_apply_and_record_are_the_choices_abstract_methods(self):
        self.assertEqual(Choice.__abstractmethods__, frozenset({"apply", "record"}))

    def test_each_choice_applies_its_field_and_records_it_when_set(self):
        chosen = dataclasses.replace(CLAUDE, tier=2, effort="high")
        self.assertEqual(ModelChoice(3).apply(chosen), dataclasses.replace(chosen, tier=3))
        self.assertEqual(ModelChoice(None).apply(chosen), dataclasses.replace(chosen, tier=None))
        self.assertEqual(
            EffortChoice("low").apply(chosen), dataclasses.replace(chosen, effort="low")
        )
        self.assertEqual(EffortChoice(None).apply(chosen), dataclasses.replace(chosen, effort=None))
        self.assertEqual(
            [c.record() for c in (ModelChoice(3), ModelChoice(None))], [{"tier": 3}, {}]
        )
        self.assertEqual(
            [c.record() for c in (EffortChoice("low"), EffortChoice(None))], [{"effort": "low"}, {}]
        )
        self.assertEqual((CLAUDE.tier, CLAUDE.effort), (None, None))

    def test_context_and_choices_are_frozen(self):
        for record, name in (
            (CLAUDE, "client"),
            (ModelChoice(3), "tier"),
            (EffortChoice("low"), "effort"),
        ):
            with self.subTest(record=record), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")

    def test_an_error_carries_only_its_codes(self):
        error = SelectionError(SelectionFailure.SELECT, Failure.TIMEOUT)
        self.assertEqual((str(error), error.selector_reason), ("select", Failure.TIMEOUT))
        self.assertIsNone(SelectionError(SelectionFailure.INVALID).selector_reason)
        self.assertEqual(
            [f.value for f in SelectionFailure],
            ["translate", "select", "invalid", "low-confidence"],
        )


FLOOR = 0.5


def model_layer(selector=None, floor=FLOOR):
    selector = FakeSelector() if selector is None else selector
    return ModelResolver(selector, task_tiers={}, min_confidence=floor)


def effort_layer(selector=None, floor=FLOOR):
    return EffortResolver(FakeSelector() if selector is None else selector, min_confidence=floor)


LAYERS = {"model": model_layer, "effort": effort_layer}


class LayerCase(Case):
    def write_local(self, text):
        os.makedirs(self.agent_pm, exist_ok=True)
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def ask(self, layer, context=CLAUDE, picks=()):
        """layer's choice for STATE in context over a FakeSelector picking picks, and the one
        question it asked with that state.
        """
        selector = FakeSelector(picks)
        choice = layer(selector).choose(STATE, context)
        ((state, question),) = selector.calls
        self.assertIs(state, STATE)
        return choice, question


class ModelLayer(LayerCase):
    def test_the_instructions_score_the_capability_the_work_needs(self):
        self.assertEqual(
            MODEL_INSTRUCTIONS,
            "Score how capable a model this issue's work needs: the lowest level that can still "
            "complete it to the standard it sets. The levels run from the least to the most "
            "capable model.",
        )

    def test_a_score_question_from_weakest_to_strongest(self):
        tiers = session_clients()["claude"]["tiers"]
        models = [tiers[t] for t in sorted(tiers, key=int)]
        _, question = self.ask(model_layer)
        self.assertEqual((question.instructions, question.kind), (MODEL_INSTRUCTIONS, Kind.SCORE))
        self.assertEqual(
            list(question.options.items()),
            [
                (models[3], "tier 4, the least capable and least costly"),
                (models[2], "tier 3, more capable and more costly than the levels before it"),
                (models[1], "tier 2, more capable and more costly than the levels before it"),
                (models[0], "tier 1, the most capable and most costly"),
            ],
        )

    def test_the_pick_is_its_models_lowest_tier(self):
        tiers = session_clients()["claude"]["tiers"]
        choice, _ = self.ask(model_layer, picks=[tiers["3"]])
        self.assertEqual(choice, ModelChoice(3))
        self.write_local(
            '[session.clients.opencode.tiers]\n1 = "big:1"\n2 = "big:1"\n3 = "small:1"\n4 = "small:1"\n'
        )
        choice, question = self.ask(model_layer, OPENCODE, picks=["small:1"])
        self.assertEqual(choice, ModelChoice(3))
        self.assertEqual(
            list(question.options.items()),
            [
                ("small:1", "tier 3, the least capable and least costly"),
                ("big:1", "tier 1, the most capable and most costly"),
            ],
        )

    def test_opencodes_single_model_is_offered_once_and_sends_nothing(self):
        model = session_clients()["opencode"]["tiers"]["1"]
        choice, question = self.ask(model_layer, OPENCODE)
        self.assertEqual(choice, ModelChoice(1))
        self.assertEqual(
            dict(question.options), {model: "tier 1, the least capable and least costly"}
        )
        self.assertIs(question.kind, Kind.CHOICE)  # a score question needs two levels
        # A window no request fits and no key command: a call would fail.
        jev = JevSelector(
            SelectorConfig(Backend.JEV, "http://127.0.0.1:9/v1", "jev-1.13.0", 5.0, 1, ())
        )
        with (
            mock.patch("subprocess.run") as run,
            mock.patch("urllib.request.OpenerDirector.open") as post,
        ):
            self.assertEqual(model_layer(jev).choose(STATE, OPENCODE), ModelChoice(1))
        run.assert_not_called()
        post.assert_not_called()

    def test_the_fallback_is_the_task_tiers_tier(self):
        resolver = ModelResolver(FakeSelector(), task_tiers={"light-build": 3}, min_confidence=0.5)
        self.assertEqual(
            resolver.fallback(Context("engineer", "light-build", "claude")), ModelChoice(3)
        )
        self.assertEqual(resolver.fallback(CLAUDE), ModelChoice(None))
        self.assertEqual(resolver.fallback(Context("pm", None, "codex")), ModelChoice(None))
        self.assertEqual(resolver.fallback(CLAUDE).record(), {})


class EffortLayer(LayerCase):
    def test_the_instructions_score_the_effort_the_model_needs(self):
        self.assertEqual(
            EFFORT_INSTRUCTIONS.format(model="sonnet"),
            "The run uses the model sonnet. Score the reasoning effort that model needs to "
            "complete this issue's work to the standard it sets: the lowest level that suffices.",
        )
        self.assertEqual(
            EFFORT_INSTRUCTIONS_NO_MODEL,
            "Score the reasoning effort the run's model needs to complete this issue's work to "
            "the standard it sets: the lowest level that suffices.",
        )

    def test_a_score_question_from_low_to_max(self):
        efforts = list(session_clients()["claude"]["efforts"])
        self.assertEqual(efforts, list(EFFORTS))
        _, question = self.ask(effort_layer)
        self.assertIs(question.kind, Kind.SCORE)
        self.assertEqual(
            list(question.options.items()),
            [
                ("low", "the least reasoning: fastest and cheapest"),
                ("medium", "more reasoning than the levels before it"),
                ("high", "more reasoning than the levels before it"),
                ("xhigh", "more reasoning than the levels before it"),
                ("max", "the most reasoning: slowest and costliest"),
            ],
        )
        table = {"tiers": {"1": "m"}, "efforts": {"high": "high", "low": "low"}}
        with mock.patch("tomllib.load", return_value={"clients": {"claude": table}}):
            _, question = self.ask(effort_layer)
        self.assertEqual(
            dict(question.options),
            {
                "high": "the least reasoning: fastest and cheapest",
                "low": "the most reasoning: slowest and costliest",
            },
        )

    def test_the_instructions_name_the_model_of_the_contexts_tier(self):
        tiers = session_clients()["claude"]["tiers"]
        for tier in (1, 3):
            with self.subTest(tier=tier):
                _, question = self.ask(effort_layer, dataclasses.replace(CLAUDE, tier=tier))
                self.assertEqual(
                    question.instructions, EFFORT_INSTRUCTIONS.format(model=tiers[str(tier)])
                )

    def test_no_tier_or_one_outside_the_clients_tiers_names_no_model(self):
        for tier in (None, 7):
            with self.subTest(tier=tier):
                _, question = self.ask(effort_layer, dataclasses.replace(CLAUDE, tier=tier))
                self.assertEqual(question.instructions, EFFORT_INSTRUCTIONS_NO_MODEL)

    def test_the_pick_is_the_effort(self):
        choice, _ = self.ask(effort_layer, picks=["high"])
        self.assertEqual(choice, EffortChoice("high"))

    def test_the_fallback_is_the_roles_effort(self):
        resolver = effort_layer()
        for context in (CLAUDE, dataclasses.replace(CLAUDE, tier=2, effort="max")):
            with self.subTest(context=context):
                self.assertEqual(resolver.fallback(context), EffortChoice(None))
        self.assertEqual(resolver.fallback(CLAUDE).record(), {})


class LayerFailures(LayerCase):
    def assert_invalid(self, make, context=CLAUDE):
        with self.assertRaises(SelectionError) as caught:
            make().choose(STATE, context)
        self.assertEqual(caught.exception.reason, SelectionFailure.INVALID)
        self.assertTrue(
            caught.exception.__suppress_context__ or caught.exception.__context__ is None
        )

    def test_a_state_without_fields_or_not_a_state_is_refused(self):
        for name, make in LAYERS.items():
            for state in (SelectorState({}), "Fix a typo"):
                with self.subTest(name, state=state), self.assertRaises(ValueError):
                    make().choose(state, CLAUDE)

    def test_empty_or_non_table_tiers_or_efforts_are_invalid(self):
        for name, make in LAYERS.items():
            for local in ("efforts = []", "tiers = []", "tiers = [1]"):
                with self.subTest(name, local=local):
                    self.write_local(f"[session.clients.claude]\n{local}\n")
                    self.assert_invalid(make)

    def test_empty_or_bad_tiers_or_efforts_tables_are_invalid(self):
        for name, make in LAYERS.items():
            for table in (
                {"tiers": {}, "efforts": {"low": "low"}},
                {"tiers": {"1": "m"}, "efforts": {}},
                {"tiers": {"one": "m"}, "efforts": {"low": "low"}},
                {"tiers": {"1": 2}, "efforts": {"low": "low"}},
                {"tiers": {"1": " "}, "efforts": {"low": "low"}},
                {"tiers": {"1": "m"}},
            ):
                with (
                    self.subTest(name, table=table),
                    mock.patch("tomllib.load", return_value={"clients": {"claude": table}}),
                ):
                    self.assert_invalid(make)

    def test_a_client_without_tiers_is_invalid(self):
        for name, make in LAYERS.items():
            with self.subTest(name):
                self.assert_invalid(make, Context("engineer", None, "no-such-client"))

    def test_a_failed_selector_is_a_select_error_keeping_its_code(self):
        for name, make in LAYERS.items():
            selector = FakeSelector(error=SelectorError(Failure.NO_KEY))
            with self.subTest(name), self.assertRaises(SelectionError) as caught:
                make(selector).choose(STATE, CLAUDE)
            error = caught.exception
            self.assertEqual(
                (error.reason, error.selector_reason), (SelectionFailure.SELECT, Failure.NO_KEY)
            )
            self.assertTrue(error.__suppress_context__)

    def test_a_pick_outside_the_options_or_not_an_answer_is_invalid(self):
        class Stray(FakeSelector):
            def __init__(self, answer):
                super().__init__()
                self.answer = answer

            def select(self, state, question):
                return self.answer

        for name, make in LAYERS.items():
            for answer in (Answer("not an option", 1.0), "low"):
                with self.subTest(name, answer=answer):
                    self.assert_invalid(lambda make=make, answer=answer: make(Stray(answer)))

    def test_more_than_ten_levels_are_invalid(self):
        many = {str(i): f"model-{i}" for i in range(1, 12)}
        for name, table in (
            ("model", {"tiers": many, "efforts": {"low": "low"}}),
            ("effort", {"tiers": {"1": "m"}, "efforts": {f"e{i}": "e" for i in range(11)}}),
        ):
            with (
                self.subTest(name),
                mock.patch("tomllib.load", return_value={"clients": {"claude": table}}),
            ):
                self.assert_invalid(LAYERS[name])


class Confidence(LayerCase):
    def test_an_answer_below_the_floor_is_low_confidence(self):
        for name, make in LAYERS.items():
            for confidence, floor in ((0.49, 0.5), (0.0, 0.1), (0.99, 1.0)):
                selector = FakeSelector(confidence=confidence)
                with (
                    self.subTest(name, confidence=confidence),
                    self.assertRaises(SelectionError) as caught,
                ):
                    make(selector, floor).choose(STATE, CLAUDE)
                error = caught.exception
                self.assertEqual(
                    (error.reason, error.selector_reason, str(error)),
                    (SelectionFailure.LOW_CONFIDENCE, None, "low-confidence"),
                )
                self.assertEqual(len(selector.calls), 1)

    def test_an_answer_at_or_above_the_floor_is_the_choice(self):
        for confidence, floor in ((0.5, 0.5), (1.0, 0.5), (0.0, 0.0), (1.0, 1.0)):
            with self.subTest(confidence=confidence, floor=floor):
                selector = FakeSelector(["high"], confidence=confidence)
                self.assertEqual(
                    effort_layer(selector, floor).choose(STATE, CLAUDE), EffortChoice("high")
                )
                self.assertEqual(model_layer(selector, floor).choose(STATE, CLAUDE).tier, 4)

    def test_an_answer_without_a_confidence_passes(self):
        for name, make in LAYERS.items():
            with self.subTest(name):
                make(FakeSelector(confidence=None), 1.0).choose(STATE, CLAUDE)
        # one model: the FakeSelector answers without a confidence, as Jev does unasked
        doubtful = FakeSelector(confidence=0.0)
        self.assertEqual(model_layer(doubtful, 1.0).choose(STATE, OPENCODE), ModelChoice(1))


class ModelResolverContract(ModelChoiceContract, Case):
    def make(self):
        return model_layer()


class FakeModelResolverContract(ModelChoiceContract, Case):
    def make(self):
        return FakeResolver(default=ModelChoice(2))


class EffortResolverContract(EffortChoiceContract, Case):
    def make(self):
        return effort_layer()


class FakeEffortResolverContract(EffortChoiceContract, Case):
    def make(self):
        return FakeResolver(default=EffortChoice("high"))


@dataclasses.dataclass(frozen=True)
class ClientPick(Choice):
    """Another Choice kind, as a later client step's."""

    client: str

    def apply(self, context):
        return dataclasses.replace(context, client=self.client)

    def record(self):
        return {"client": self.client}


class FakeClientResolverContract(ResolverContract, Case):
    def make(self):
        return FakeResolver(default=ClientPick("codex"))


class FakeScript(Case):
    def test_a_scripted_choice_wins_for_its_state(self):
        other = state_of({**TRANSLATION, "title": "other"})
        fake = FakeResolver(default=ModelChoice(2), choices=[(STATE, ModelChoice(3))])
        self.assertEqual(fake.choose(state_of(), CLAUDE), ModelChoice(3))
        self.assertEqual(fake.choose(other, CLAUDE), ModelChoice(2))
        self.assertEqual(fake.calls, [(STATE, CLAUDE), (other, CLAUDE)])

    def test_a_scripted_error_is_raised(self):
        error = SelectionError(SelectionFailure.SELECT)
        with self.assertRaises(SelectionError) as caught:
            FakeResolver(default=ClientPick("x"), error=error).choose(STATE, CLAUDE)
        self.assertIs(caught.exception, error)

    def test_the_fallback_is_its_scripted_one_else_the_default(self):
        fake = FakeResolver(default=EffortChoice("high"), fallback=EffortChoice(None))
        self.assertEqual(fake.fallback(CLAUDE), EffortChoice(None))
        self.assertEqual(FakeResolver(default=ClientPick("x")).fallback(CLAUDE), ClientPick("x"))


if __name__ == "__main__":
    unittest.main()
