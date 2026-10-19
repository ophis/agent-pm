"""FakeResolver, a Resolver that asks no Selector, and the contracts a Resolver passes: ResolverContract
(hooks make() -> Resolver, context() -> Context) and TierChoiceContract (adds the TierChoice checks).
A contract subclass also inherits Case.
"""

from collections.abc import Mapping
from typing import Generic, TypeVar

from agent_pm.linear_orchestrator.engine.selection import (
    Choice,
    Context,
    Resolver,
    TierChoice,
)

C = TypeVar("C", bound=Choice)

STATE = "Fix a typo in the README."
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class FakeResolver(Resolver[C], Generic[C]):
    """choose returns choices[state] when given, else default; error, when given, is raised
    instead; fallback returns fallback (default when not given); calls records each (state,
    context).
    """

    def __init__(
        self,
        *,
        default: C,
        fallback: C | None = None,
        choices: Mapping[str, C] | None = None,
        error: Exception | None = None,
    ):
        self.default = default
        self.fallback_choice = default if fallback is None else fallback
        self.choices = dict(choices or {})
        self.error = error
        self.calls: list[tuple[str, Context]] = []

    def choose(self, state: str, context: Context) -> C:
        self.calls.append((state, context))
        if not state.strip():
            raise ValueError("state: want a non-blank state")
        if self.error is not None:
            raise self.error
        return self.choices.get(state, self.default)

    def fallback(self, context: Context) -> C:
        return self.fallback_choice


class ResolverContract:
    def make(self) -> Resolver:
        raise NotImplementedError

    def context(self) -> Context:
        return Context("engineer", "build", "claude")

    def test_a_blank_state_is_refused(self):
        with self.assertRaises(ValueError):
            self.make().choose("  ", self.context())

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


class TierChoiceContract(ResolverContract):
    def test_the_choice_is_a_tier_and_an_effort(self):
        choice = self.make().choose(STATE, self.context())
        self.assertIsInstance(choice, TierChoice)
        self.assertIn(choice.tier, range(1, 5))
        self.assertIn(choice.effort, EFFORTS)
        applied = choice.apply(self.context())
        self.assertEqual((applied.tier, applied.effort), (choice.tier, choice.effort))
        self.assertEqual(choice.record(), {"tier": choice.tier, "effort": choice.effort})
