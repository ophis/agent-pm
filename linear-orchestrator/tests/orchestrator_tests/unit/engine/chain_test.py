"""engine/chain.py: resolve over FakeResolvers and a fake translator Delegate (no chain, fallbacks,
order, records, reason-code-only logs), steps over the configured Selector class.
"""

import os
from dataclasses import dataclass, replace
from types import SimpleNamespace

import agent_pm.session as session
from agent_pm import utils
from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.engine import chain
from agent_pm.linear_orchestrator.engine.selection import (
    EFFORT_INSTRUCTIONS,
    MODEL_INSTRUCTIONS,
    Choice,
    EffortChoice,
    EffortResolver,
    ModelChoice,
    ModelResolver,
    SelectionError,
    SelectionFailure,
)
from agent_pm.linear_orchestrator.engine.types import Context, ResolverName, Selection
from agent_pm.selector import Failure, SelectorError
from orchestrator_tests.case import Case
from orchestrator_tests.selection_fixtures import (
    TRANSLATION,
    FakeResolver,
    deliverable,
    state_of,
)
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.run_fixtures import logged

from delegate.tests.fakes.delegate import FakeDelegate
from selector.tests.fakes.selector import FakeSelector

LEAKY = {**TRANSLATION, "title": "LEAK-STATE a docs fix"}
STATE = state_of(LEAKY)
DELIVERABLE = deliverable(LEAKY)
LEAK = "LEAK-SENTINEL"
CONTEXT = Context("engineer", "build", "claude")
PICKED = ModelChoice(1)
FALLBACK = ModelChoice(3)


def translator(text=DELIVERABLE, *, run_error=None):
    """A FakeDelegate class whose runs end done with text as their deliverable, or raise run_error;
    each create is recorded.
    """

    class Translator(FakeDelegate):
        ROLES = {"choice-translator": {"translate": "Translates an issue into a state"}}
        outcome_result = Outcome("done", "Translated", "translated", deliverable=text)
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


def model(**kw):
    return chain.Step(ResolverName.MODEL, FakeResolver(default=PICKED, fallback=FALLBACK, **kw))


def client(name):
    return chain.Step("client", FakeResolver(default=ClientChoice(name)))


class Unbuildable(FakeSelector):
    def __init__(self):
        raise AssertionError("no chain builds no Selector")


class FailingOn(FakeSelector):
    """A FakeSelector raising error for the question whose instructions are failing."""

    def __init__(self, picks, *, failing, error):
        super().__init__(picks)
        self.failing, self.failing_error = failing, error

    def select(self, state, question):
        if question.instructions == self.failing:
            self.calls.append((state, question))
            raise self.failing_error
        return super().select(state, question)


def layers(selector):
    """The model and effort Steps over selector, with [task_tiers] build = 2."""
    cfg = SimpleNamespace(
        task_tiers={"build": 2},
        selection=Selection(FakeSelector, (ResolverName.MODEL, ResolverName.EFFORT)),
    )
    return chain.steps(cfg, selector)


class Resolving(Case):
    def setUp(self):
        super().setUp()
        self.logs = os.path.join(self.home, "logs")
        self.workdir = os.path.join(self.home, "work")
        os.makedirs(self.workdir)
        self.provider = translator()

    def resolve(self, steps, *, provider=None):
        with logs_at(self.logs):
            return chain.resolve(
                "TASK-1",
                self.workdir,
                CONTEXT,
                steps,
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

    def truncate_log(self):
        os.makedirs(self.logs, exist_ok=True)
        with open(os.path.join(self.logs, "orchestrator.jsonl"), "w"):
            pass

    def claude_tiers(self):
        """Session's merged claude tiers: tier string to model."""
        return utils.read_resources(session.CONFIG_RESOURCES)["clients"]["claude"]["tiers"]

    def assert_no_leak(self):
        with open(os.path.join(self.logs, "orchestrator.jsonl"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("LEAK", text)

    def test_no_resolvers_is_no_chain(self):
        self.assertEqual(self.resolve(()), (CONTEXT, {}))
        self.assertEqual((self.provider.created, self.choices()), ([], []))

    def test_active_chain_translates_once_and_applies_the_choice(self):
        step = model()
        self.assertEqual(self.resolve((step,)), (replace(CONTEXT, tier=1), {"tier": 1}))
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
                    "step": "model",
                    "by": "selector",
                    "choice": {"tier": 1},
                }
            ],
        )
        self.assert_no_leak()

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
                    self.resolve((model(error=error),)), (replace(CONTEXT, tier=3), {"tier": 3})
                )
                self.assertEqual(
                    self.choices(),
                    [
                        {
                            "kind": "choice",
                            "issue": "TASK-1",
                            "step": "model",
                            "by": "fallback",
                            **codes,
                            "choice": {"tier": 3},
                        }
                    ],
                )
                self.assert_no_leak()

    def test_any_other_choose_error_falls_back_as_error(self):
        self.assertEqual(
            self.resolve((model(error=KeyError(LEAK)),)), (replace(CONTEXT, tier=3), {"tier": 3})
        )
        self.assertEqual([(e["by"], e["reason"]) for e in self.choices()], [("fallback", "error")])
        self.assert_no_leak()

    def test_translate_failure_falls_back_every_step(self):
        for name, provider, reason in (
            ("failed run", translator(run_error=RuntimeError(LEAK)), "translate"),
            ("blank state", translator(" \n"), "invalid"),
            ("state text", translator("LEAK-STATE a docs fix"), "invalid"),
        ):
            with self.subTest(name):
                os.makedirs(self.logs, exist_ok=True)
                with open(os.path.join(self.logs, "orchestrator.jsonl"), "w"):
                    pass
                first, second = model(), client("x")
                self.assertEqual(
                    self.resolve((first, second), provider=provider),
                    (replace(CONTEXT, tier=3, client="x"), {"tier": 3, "client": "x"}),
                )
                self.assertEqual((first.resolver.calls, second.resolver.calls), ([], []))
                self.assertEqual(
                    [(e["step"], e["by"], e.get("reason")) for e in self.choices()],
                    [
                        ("translate", "fallback", reason),
                        ("model", "fallback", "no-state"),
                        ("client", "fallback", "no-state"),
                    ],
                )
                self.assert_no_leak()

    def test_steps_run_in_order_on_the_applied_context(self):
        failing = SelectionError(SelectionFailure.INVALID)
        for first, second, context, records, seen in (
            (
                client("codex"),
                model(),
                replace(CONTEXT, client="codex", tier=1),
                {"client": "codex", "tier": 1},
                replace(CONTEXT, client="codex"),
            ),
            (
                model(error=failing),
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
            self.resolve((model(), model()))
        self.assertEqual(str(caught.exception), "choice record field tier recorded twice")

    def test_each_layer_falls_back_on_its_own(self):
        """One layer's failure falls back for it alone; the other still asks the Selector, the
        effort question naming the model the model layer left.
        """
        timeout = SelectorError(Failure.TIMEOUT)
        claude = self.claude_tiers()
        cases = (
            (
                "model fails",
                MODEL_INSTRUCTIONS,
                timeout,
                (replace(CONTEXT, tier=2, effort="high"), {"tier": 2, "effort": "high"}),
                [
                    ("model", "fallback", "select", "timeout", {"tier": 2}),
                    ("effort", "selector", None, None, {"effort": "high"}),
                ],
                EFFORT_INSTRUCTIONS.format(model=claude["2"]),
            ),
            (
                "effort fails",
                EFFORT_INSTRUCTIONS.format(model=claude["3"]),
                timeout,
                (replace(CONTEXT, tier=3), {"tier": 3}),
                [
                    ("model", "selector", None, None, {"tier": 3}),
                    ("effort", "fallback", "select", "timeout", {}),
                ],
                EFFORT_INSTRUCTIONS.format(model=claude["3"]),
            ),
            (
                "effort breaks",
                EFFORT_INSTRUCTIONS.format(model=claude["3"]),
                RuntimeError(LEAK),
                (replace(CONTEXT, tier=3), {"tier": 3}),
                [
                    ("model", "selector", None, None, {"tier": 3}),
                    ("effort", "fallback", "error", None, {}),
                ],
                EFFORT_INSTRUCTIONS.format(model=claude["3"]),
            ),
        )
        for name, failing, error, resolved, lines, effort_asked in cases:
            with self.subTest(name):
                self.truncate_log()
                selector = FailingOn([claude["3"], "high"], failing=failing, error=error)
                self.assertEqual(self.resolve(layers(selector)), resolved)
                self.assertEqual(
                    [q.instructions for _, q in selector.calls], [MODEL_INSTRUCTIONS, effort_asked]
                )
                want = [
                    {
                        "kind": "choice",
                        "issue": "TASK-1",
                        "step": step,
                        "by": by,
                        **({} if reason is None else {"reason": reason}),
                        **({} if code is None else {"selector": code}),
                        "choice": record,
                    }
                    for step, by, reason, code, record in lines
                ]
                self.assertEqual(self.choices(), want)
                self.assert_no_leak()

    def test_no_state_falls_back_in_both_layers(self):
        selector = FakeSelector()
        self.assertEqual(
            self.resolve(layers(selector), provider=translator(" \n")),
            (replace(CONTEXT, tier=2), {"tier": 2}),
        )
        self.assertEqual(selector.calls, [])
        self.assertEqual(
            [(e["step"], e["by"], e.get("reason"), e.get("choice")) for e in self.choices()],
            [
                ("translate", "fallback", "invalid", None),
                ("model", "fallback", "no-state", {"tier": 2}),
                ("effort", "fallback", "no-state", {}),
            ],
        )

    def test_steps_build_no_selector_without_resolvers(self):
        cfg = SimpleNamespace(
            task_tiers={}, selection=Selection(selector=Unbuildable, resolvers=())
        )
        self.assertEqual(chain.steps(cfg), ())

    def test_steps_build_the_configured_selector(self):
        cfg = SimpleNamespace(
            task_tiers={"build": 2},
            selection=Selection(
                selector=FakeSelector, resolvers=(ResolverName.MODEL, ResolverName.EFFORT)
            ),
        )
        injected = FakeSelector()
        for given in (None, injected):
            with self.subTest(injected=given is not None):
                model_step, effort_step = chain.steps(cfg, given)
                self.assertEqual(
                    (model_step.name, effort_step.name), (ResolverName.MODEL, ResolverName.EFFORT)
                )
                self.assertIsInstance(model_step.resolver, ModelResolver)
                self.assertIsInstance(effort_step.resolver, EffortResolver)
                for step in (model_step, effort_step):
                    self.assertIsInstance(step.resolver.selector, FakeSelector)
                    if given is not None:
                        self.assertIs(step.resolver.selector, given)
                self.assertEqual(model_step.resolver.fallback(CONTEXT), ModelChoice(2))
                self.assertEqual(effort_step.resolver.fallback(CONTEXT), EffortChoice(None))
