"""engine/selection.py: translate over a fake Delegate, the chain's Context and Resolver, and the TierResolver over
session's merged tier config; FakeResolver passes the resolver contracts.
"""

import dataclasses
import inspect
import os
import unittest

import agent_pm.session as session
from agent_pm import utils
from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.engine.selection import (
    Choice,
    Context,
    Resolver,
    SelectionError,
    SelectionFailure,
    TierChoice,
    TierResolver,
    translate,
)
from agent_pm.selector import Failure, SelectorError
from orchestrator_tests.case import Case
from orchestrator_tests.selection_fixtures import (
    STATE,
    FakeResolver,
    ResolverContract,
    TierChoiceContract,
)

from delegate.tests.fakes.delegate import FakeDelegate
from selector.tests.fakes.selector import FakeSelector

CLAUDE = Context("engineer", "build", "claude")
OPENCODE = Context("engineer", "build", "opencode")


def session_clients() -> dict:
    """Session's own merged parse of its config (the reference the TierResolver must match)."""
    return utils.read_resources(session.CONFIG_RESOURCES)["clients"]


def provider(outcome=None, *, error=None):
    """A FakeDelegate class whose runs end in outcome (or raise error), recording each create."""

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

    return Translator


def done(deliverable):
    return Outcome("done", "Translated", "translated the issue", deliverable=deliverable)


class Translating(Case):
    def workdir(self):
        path = os.path.join(self.home, "translate")
        os.makedirs(path, exist_ok=True)
        return path

    def test_one_headless_run_of_the_role_whose_deliverable_is_the_state(self):
        fake = provider(done("  a docs fix \n"))
        core = {"output": "x"}
        workdir = self.workdir()
        self.assertEqual(
            translate("TASK-1", workdir, provider=fake, overlays=(core,)), "a docs fix"
        )
        ((role, spec, overlays),) = fake.created
        self.assertEqual(role, "choice-translator")
        self.assertEqual(
            (spec.input, spec.workdir, spec.task, spec.client, spec.mode, spec.resume),
            ("TASK-1", workdir, "translate", None, "headless", False),
        )
        self.assertEqual(overlays, (core,))

    def test_a_blank_issue_is_refused_before_any_run(self):
        fake = provider(done("x"))
        with self.assertRaises(ValueError):
            translate(" ", self.workdir(), provider=fake)
        self.assertEqual(fake.created, [])

    def test_a_failed_run_is_a_translate_error_without_its_text(self):
        fake = provider(error=RuntimeError("secret issue text"))
        with self.assertRaises(SelectionError) as caught:
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
            self.assertEqual(caught.exception.reason, SelectionFailure.TRANSLATE)

    def test_a_blank_deliverable_is_invalid(self):
        with self.assertRaises(SelectionError) as caught:
            translate("TASK-1", self.workdir(), provider=provider(done(" \n")))
        self.assertEqual(caught.exception.reason, SelectionFailure.INVALID)


class Types(Case):
    def test_choose_and_fallback_are_the_resolvers_abstract_methods(self):
        self.assertEqual(Resolver.__abstractmethods__, frozenset({"choose", "fallback"}))
        self.assertEqual(
            list(inspect.signature(Resolver.choose).parameters), ["self", "state", "context"]
        )
        self.assertEqual(list(inspect.signature(Resolver.fallback).parameters), ["self", "context"])

    def test_apply_and_record_are_the_choices_abstract_methods(self):
        self.assertEqual(Choice.__abstractmethods__, frozenset({"apply", "record"}))

    def test_a_tier_choice_applies_itself_and_records_what_it_sets(self):
        self.assertEqual(
            TierChoice(3, "low").apply(CLAUDE), dataclasses.replace(CLAUDE, tier=3, effort="low")
        )
        self.assertEqual(TierChoice(3, "low").record(), {"tier": 3, "effort": "low"})
        self.assertEqual(TierChoice(None, None).record(), {})
        self.assertEqual((CLAUDE.tier, CLAUDE.effort), (None, None))

    def test_context_and_tier_choice_are_frozen(self):
        field = "client"
        for record, name in ((CLAUDE, field), (TierChoice(3, "low"), "tier")):
            with self.subTest(record=record), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")

    def test_an_error_carries_only_its_codes(self):
        error = SelectionError(SelectionFailure.SELECT, Failure.TIMEOUT)
        self.assertEqual((str(error), error.selector_reason), ("select", Failure.TIMEOUT))
        self.assertIsNone(SelectionError(SelectionFailure.INVALID).selector_reason)
        self.assertEqual([f.value for f in SelectionFailure], ["translate", "select", "invalid"])


class TierOptions(Case):
    def test_one_option_per_model_and_effort_from_the_claude_tiers(self):
        clients = session_clients()
        tiers = clients["claude"]["tiers"]
        efforts = list(clients["claude"]["efforts"])
        models = [tiers[t] for t in sorted(tiers, key=int)]
        selector = FakeSelector()
        TierResolver(selector, task_tiers={}).choose(STATE, CLAUDE)
        ((state, options),) = selector.calls
        self.assertEqual(state, STATE)
        self.assertEqual(options, tuple(f"{m} / {e}" for m in models for e in efforts))

    def test_the_pick_maps_back_to_its_models_lowest_tier(self):
        tiers = session_clients()["claude"]["tiers"]
        tier = sorted(tiers, key=int)[2]
        selector = FakeSelector({STATE: f"{tiers[tier]} / low"})
        self.assertEqual(
            TierResolver(selector, task_tiers={}).choose(STATE, CLAUDE),
            TierChoice(int(tier), "low"),
        )

    def test_tiers_naming_one_model_offer_it_once_under_its_lowest_tier(self):
        model = session_clients()["opencode"]["tiers"]["1"]
        selector = FakeSelector({STATE: f"{model} / high"})
        self.assertEqual(
            TierResolver(selector, task_tiers={}).choose(STATE, OPENCODE), TierChoice(1, "high")
        )
        ((_, options),) = selector.calls
        self.assertEqual({o.split(" / ")[0] for o in options}, {model})


class LocalOverride(Case):
    def write_local(self, text):
        os.makedirs(self.agent_pm, exist_ok=True)
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_the_local_config_overrides_the_opencode_tiers(self):
        self.write_local(
            '[session.clients.opencode.tiers]\n1 = "big:1"\n2 = "big:1"\n3 = "small:1"\n4 = "small:1"\n'
        )
        selector = FakeSelector({STATE: "small:1 / low"})
        self.assertEqual(
            TierResolver(selector, task_tiers={}).choose(STATE, OPENCODE), TierChoice(3, "low")
        )
        ((_, options),) = selector.calls
        self.assertEqual({o.split(" / ")[0] for o in options}, {"big:1", "small:1"})
        self.assertEqual(session_clients()["opencode"]["tiers"]["3"], "small:1")


class TierFailures(Case):
    def test_a_blank_state_is_refused(self):
        with self.assertRaises(ValueError):
            TierResolver(FakeSelector(), task_tiers={}).choose(" ", CLAUDE)

    def test_a_client_without_tiers_is_invalid(self):
        with self.assertRaises(SelectionError) as caught:
            TierResolver(FakeSelector(), task_tiers={}).choose(
                STATE, Context("engineer", None, "no-such-client")
            )
        self.assertEqual(caught.exception.reason, SelectionFailure.INVALID)

    def test_a_failed_selector_is_a_select_error_keeping_its_code(self):
        selector = FakeSelector(error=SelectorError(Failure.NO_KEY))
        with self.assertRaises(SelectionError) as caught:
            TierResolver(selector, task_tiers={}).choose(STATE, CLAUDE)
        error = caught.exception
        self.assertEqual(
            (error.reason, error.selector_reason), (SelectionFailure.SELECT, Failure.NO_KEY)
        )
        self.assertTrue(error.__suppress_context__)

    def test_a_pick_outside_the_options_is_invalid(self):
        class Stray(FakeSelector):
            def select(self, state, choices):
                return "not an option"

        with self.assertRaises(SelectionError) as caught:
            TierResolver(Stray(), task_tiers={}).choose(STATE, CLAUDE)
        self.assertEqual(caught.exception.reason, SelectionFailure.INVALID)


class TierFallback(Case):
    def test_the_fallback_is_the_task_tiers_tier_and_the_roles_effort(self):
        resolver = TierResolver(FakeSelector(), task_tiers={"light-build": 3})
        self.assertEqual(
            resolver.fallback(Context("engineer", "light-build", "claude")), TierChoice(3, None)
        )
        self.assertEqual(resolver.fallback(CLAUDE), TierChoice(None, None))
        self.assertEqual(resolver.fallback(Context("pm", None, "codex")), TierChoice(None, None))


class TierResolverContract(TierChoiceContract, Case):
    def make(self):
        return TierResolver(FakeSelector(), task_tiers={})


class FakeTierResolverContract(TierChoiceContract, Case):
    def make(self):
        return FakeResolver(default=TierChoice(2, "high"))


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
        fake = FakeResolver(
            default=TierChoice(2, "high"), choices={"a docs fix": TierChoice(3, "low")}
        )
        self.assertEqual(fake.choose("a docs fix", CLAUDE), TierChoice(3, "low"))
        self.assertEqual(fake.choose("other", CLAUDE), TierChoice(2, "high"))
        self.assertEqual(fake.calls, [("a docs fix", CLAUDE), ("other", CLAUDE)])

    def test_a_scripted_error_is_raised(self):
        error = SelectionError(SelectionFailure.SELECT)
        with self.assertRaises(SelectionError) as caught:
            FakeResolver(default=ClientPick("x"), error=error).choose(STATE, CLAUDE)
        self.assertIs(caught.exception, error)

    def test_the_fallback_is_its_scripted_one_else_the_default(self):
        fake = FakeResolver(default=TierChoice(2, "high"), fallback=TierChoice(None, None))
        self.assertEqual(fake.fallback(CLAUDE), TierChoice(None, None))
        self.assertEqual(FakeResolver(default=ClientPick("x")).fallback(CLAUDE), ClientPick("x"))


if __name__ == "__main__":
    unittest.main()
