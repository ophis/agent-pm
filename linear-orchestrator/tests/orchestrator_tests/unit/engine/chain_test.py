"""engine/chain.py: resolve over FakeResolvers and a fake translator Delegate (no chain, fallbacks,
order, records, reason-code-only logs), steps over the configured Selector class.
"""

import os
from dataclasses import dataclass, replace
from types import SimpleNamespace

from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.engine import chain
from agent_pm.linear_orchestrator.engine.selection import (
    Choice,
    SelectionError,
    SelectionFailure,
    TierChoice,
    TierResolver,
)
from agent_pm.linear_orchestrator.engine.types import Context, ResolverName, Selection
from agent_pm.selector import Failure
from orchestrator_tests.case import Case
from orchestrator_tests.selection_fixtures import FakeResolver
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.run_fixtures import logged

from delegate.tests.fakes.delegate import FakeDelegate
from selector.tests.fakes.selector import FakeSelector

STATE = "LEAK-STATE a docs fix"
LEAK = "LEAK-SENTINEL"
CONTEXT = Context("engineer", "build", "claude")
PICKED = TierChoice(1, "low")
FALLBACK = TierChoice(3, None)


def translator(deliverable=STATE, *, run_error=None):
    """A FakeDelegate class whose runs end done with deliverable, or raise run_error; each create
    is recorded.
    """

    class Translator(FakeDelegate):
        ROLES = {"choice-translator": {"translate": "Translates an issue into a state"}}
        outcome_result = Outcome("done", "Translated", "translated", deliverable=deliverable)
        created = []

        @classmethod
        def create(cls, role, spec, *, display=None, callback=None, overlays=()):
            cls.created.append((role, spec, overlays))
            return super().create(role, spec, display=display, callback=callback, overlays=overlays)

        def run(self, on_event=None):
            if run_error is not None:
                raise run_error
            return super().run(on_event)

    return Translator


@dataclass(frozen=True)
class ClientChoice(Choice):
    client: str

    def apply(self, context):
        return replace(context, client=self.client)

    def record(self):
        return {"client": self.client}


def tier(**kw):
    return chain.Step(ResolverName.TIER, FakeResolver(default=PICKED, fallback=FALLBACK, **kw))


def client(name):
    return chain.Step("client", FakeResolver(default=ClientChoice(name)))


class Unbuildable(FakeSelector):
    def __init__(self):
        raise AssertionError("no chain builds no Selector")


class Resolving(Case):
    def setUp(self):
        super().setUp()
        self.logs = os.path.join(self.home, "logs")
        self.workdir = os.path.join(self.home, "work")
        os.makedirs(self.workdir)
        self.provider = translator()

    def resolve(self, steps, *, translate_on=True, provider=None):
        with logs_at(self.logs):
            return chain.resolve(
                "TASK-1",
                self.workdir,
                CONTEXT,
                steps,
                translate_on=translate_on,
                provider=provider or self.provider,
                overlays=({"core": 1},),
            )

    def choices(self):
        """The logged choice lines, without ts and src."""
        return [
            {k: v for k, v in e.items() if k not in ("ts", "src")}
            for e in logged(self.logs)
            if e["kind"] == "choice"
        ]

    def assert_no_leak(self):
        with open(os.path.join(self.logs, "orchestrator.jsonl"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("LEAK", text)

    def test_no_resolvers_is_no_chain(self):
        for translate_on in (False, True):
            with self.subTest(translate_on=translate_on):
                self.assertEqual(self.resolve((), translate_on=translate_on), (CONTEXT, {}))
        self.assertEqual((self.provider.created, self.choices()), ([], []))

    def test_active_chain_translates_once_and_applies_the_choice(self):
        step = tier()
        self.assertEqual(
            self.resolve((step,)),
            (replace(CONTEXT, tier=1, effort="low"), {"tier": 1, "effort": "low"}),
        )
        ((role, spec, overlays),) = self.provider.created
        self.assertEqual(
            (role, spec.input, spec.workdir, spec.task, overlays),
            ("choice-translator", "TASK-1", self.workdir, "translate", ({"core": 1},)),
        )
        self.assertEqual(step.resolver.calls, [(STATE, CONTEXT)])
        self.assertEqual(
            self.choices(),
            [
                {
                    "kind": "choice",
                    "issue": "TASK-1",
                    "step": "tier",
                    "by": "selector",
                    "choice": {"tier": 1, "effort": "low"},
                }
            ],
        )
        self.assert_no_leak()

    def test_step_on_translate_off_falls_back_no_state(self):
        step = tier()
        self.assertEqual(
            self.resolve((step,), translate_on=False), (replace(CONTEXT, tier=3), {"tier": 3})
        )
        self.assertEqual((self.provider.created, step.resolver.calls), ([], []))
        self.assertEqual(
            [(e["step"], e["by"], e["reason"]) for e in self.choices()],
            [("tier", "fallback", "no-state")],
        )

    def test_selector_failure_falls_back_logging_codes_only(self):
        cases = [
            *(
                (
                    SelectionError(SelectionFailure.SELECT, failure),
                    {"reason": "select", "selector": failure.value},
                )
                for failure in Failure
            ),
            (SelectionError(SelectionFailure.INVALID), {"reason": "invalid"}),
        ]
        for error, codes in cases:
            with self.subTest(codes=codes):
                os.makedirs(self.logs, exist_ok=True)
                with open(os.path.join(self.logs, "orchestrator.jsonl"), "w"):
                    pass
                self.assertEqual(
                    self.resolve((tier(error=error),)), (replace(CONTEXT, tier=3), {"tier": 3})
                )
                self.assertEqual(
                    self.choices(),
                    [
                        {
                            "kind": "choice",
                            "issue": "TASK-1",
                            "step": "tier",
                            "by": "fallback",
                            **codes,
                            "choice": {"tier": 3},
                        }
                    ],
                )
                self.assert_no_leak()

    def test_any_other_choose_error_falls_back_as_error(self):
        self.assertEqual(
            self.resolve((tier(error=KeyError(LEAK)),)), (replace(CONTEXT, tier=3), {"tier": 3})
        )
        self.assertEqual([(e["by"], e["reason"]) for e in self.choices()], [("fallback", "error")])
        self.assert_no_leak()

    def test_translate_failure_falls_back_every_step(self):
        for name, provider, reason in (
            ("failed run", translator(run_error=RuntimeError(LEAK)), "translate"),
            ("blank state", translator(" \n"), "invalid"),
        ):
            with self.subTest(name):
                os.makedirs(self.logs, exist_ok=True)
                with open(os.path.join(self.logs, "orchestrator.jsonl"), "w"):
                    pass
                first, second = tier(), client("x")
                self.assertEqual(
                    self.resolve((first, second), provider=provider),
                    (replace(CONTEXT, tier=3, client="x"), {"tier": 3, "client": "x"}),
                )
                self.assertEqual((first.resolver.calls, second.resolver.calls), ([], []))
                self.assertEqual(
                    [(e["step"], e["by"], e.get("reason")) for e in self.choices()],
                    [
                        ("translate", "fallback", reason),
                        ("tier", "fallback", "no-state"),
                        ("client", "fallback", "no-state"),
                    ],
                )
                self.assert_no_leak()

    def test_steps_run_in_order_on_the_applied_context(self):
        failing = SelectionError(SelectionFailure.INVALID)
        for first, second, context, records, seen in (
            (
                client("codex"),
                tier(),
                replace(CONTEXT, client="codex", tier=1, effort="low"),
                {"client": "codex", "tier": 1, "effort": "low"},
                replace(CONTEXT, client="codex"),
            ),
            (
                tier(error=failing),
                client("codex"),
                replace(CONTEXT, client="codex", tier=3),
                {"tier": 3, "client": "codex"},
                replace(CONTEXT, tier=3),
            ),
        ):
            with self.subTest(first=first.name):
                self.assertEqual(self.resolve((first, second)), (context, records))
                self.assertEqual(first.resolver.calls, [(STATE, CONTEXT)])
                self.assertEqual(second.resolver.calls, [(STATE, seen)])

    def test_a_key_recorded_twice_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.resolve((tier(), tier()))
        self.assertEqual(str(caught.exception), "choice record field tier recorded twice")

    def test_steps_build_no_selector_without_resolvers(self):
        cfg = SimpleNamespace(
            task_tiers={}, selection=Selection(selector=Unbuildable, translate=True, resolvers=())
        )
        self.assertEqual(chain.steps(cfg), ())

    def test_steps_build_the_configured_selector(self):
        cfg = SimpleNamespace(
            task_tiers={"build": 2},
            selection=Selection(
                selector=FakeSelector, translate=True, resolvers=(ResolverName.TIER,)
            ),
        )
        injected = FakeSelector()
        for given, kind in ((None, FakeSelector), (injected, FakeSelector)):
            with self.subTest(injected=given is not None):
                (step,) = chain.steps(cfg, given)
                self.assertEqual(step.name, ResolverName.TIER)
                self.assertIsInstance(step.resolver, TierResolver)
                self.assertIsInstance(step.resolver.selector, kind)
                if given is not None:
                    self.assertIs(step.resolver.selector, given)
                self.assertEqual(step.resolver.fallback(CONTEXT), TierChoice(2, None))
