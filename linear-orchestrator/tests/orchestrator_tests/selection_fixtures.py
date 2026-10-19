"""FakeResolver, a Resolver that asks no Selector, and the contracts a Resolver passes: ResolverContract
(hooks make() -> Resolver, context() -> Context), ModelChoiceContract and EffortChoiceContract (add
their Choice's checks). A contract subclass also inherits Case. TRANSLATION: a translate
deliverable's JSON value, STATE its State.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Generic, TypeVar

from agent_pm.linear_orchestrator.engine.selection import (
    Choice,
    Context,
    EffortChoice,
    ModelChoice,
    Resolver,
)
from agent_pm.selector import State as SelectorState

C = TypeVar("C", bound=Choice)

TRANSLATION = {
    "title": "Fix a typo in the README",
    "description": "The word 'instal' in the README.",
    "status": "Todo",
    "labels": ["Docs"],
    "comments": [
        {"author": "A", "created_at": "2026-10-19T09:00:00Z", "body": "Also check CONTRIBUTING."}
    ],
}


def deliverable(translation: Mapping = TRANSLATION) -> str:
    """translation as the translate run's deliverable."""
    return json.dumps(translation, ensure_ascii=False)


def state_of(translation: Mapping = TRANSLATION) -> SelectorState:
    """translation (comments oldest first) as translate's State."""
    fields = {k: v for k, v in translation.items() if k != "comments"}
    return SelectorState(fields, droppable=("comments", tuple(translation["comments"])))


STATE = state_of()
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class FakeResolver(Resolver[C], Generic[C]):
    """choose returns the choice paired with an equal state in choices, else default; error, when
    given, is raised instead; fallback returns fallback (default when not given); calls records
    each (state, context).
    """

    def __init__(
        self,
        *,
        default: C,
        fallback: C | None = None,
        choices: Sequence[tuple[SelectorState, C]] = (),
        error: Exception | None = None,
    ):
        self.default = default
        self.fallback_choice = default if fallback is None else fallback
        self.choices = tuple(choices)
        self.error = error
        self.calls: list[tuple[SelectorState, Context]] = []

    def choose(self, state: SelectorState, context: Context) -> C:
        self.calls.append((state, context))
        if not isinstance(state, SelectorState) or not state.fields:
            raise ValueError("state: want a State with fields")
        if self.error is not None:
            raise self.error
        return next((c for s, c in self.choices if s == state), self.default)

    def fallback(self, context: Context) -> C:
        return self.fallback_choice


class ResolverContract:
    def make(self) -> Resolver:
        raise NotImplementedError

    def context(self) -> Context:
        return Context("engineer", "build", "claude")

    def test_a_state_without_fields_or_not_a_state_is_refused(self):
        for state in (SelectorState({}), "Fix a typo"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.make().choose(state, self.context())

    def test_the_same_state_and_context_give_the_same_choice(self):
        resolver = self.make()
        self.assertEqual(
            resolver.choose(STATE, self.context()), resolver.choose(STATE, self.context())
        )

    def test_a_choice_and_the_fallback_apply_to_a_context_and_record_a_dict(self):
        resolver = self.make()
        for choice in (resolver.choose(STATE, self.context()), resolver.fallback(self.context())):
            with self.subTest(choice=choice):
                self.assertIsInstance(choice, Choice)
                self.assertIsInstance(choice.apply(self.context()), Context)
                self.assertIsInstance(choice.record(), dict)


class ModelChoiceContract(ResolverContract):
    def test_the_choice_is_a_tier(self):
        choice = self.make().choose(STATE, self.context())
        self.assertIsInstance(choice, ModelChoice)
        self.assertIn(choice.tier, range(1, 5))
        self.assertEqual(choice.apply(self.context()), replace(self.context(), tier=choice.tier))
        self.assertEqual(choice.record(), {"tier": choice.tier})


class EffortChoiceContract(ResolverContract):
    def test_the_choice_is_an_effort(self):
        choice = self.make().choose(STATE, self.context())
        self.assertIsInstance(choice, EffortChoice)
        self.assertIn(choice.effort, EFFORTS)
        self.assertEqual(
            choice.apply(self.context()), replace(self.context(), effort=choice.effort)
        )
        self.assertEqual(choice.record(), {"effort": choice.effort})
