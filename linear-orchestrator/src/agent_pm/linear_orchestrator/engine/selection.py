"""The router's choice chain. translate turns an issue into the state once, a faithful English
rendering of its title, description, status, labels and comments; each step is a Resolver
paired with its Choice: choose picks one for the state given the Context so far, among described
options it reads from config, by asking a Selector its own question, refusing an answer below its
confidence floor; fallback gives today's choice, for a step switched off or a SelectionError. The
router applies each Choice to the Context in a fixed order and records it for resume, logging only
reason codes: issue text, the state and replies are untrusted data.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Generic, TypeVar

import agent_pm.utils as utils
from agent_pm.delegate import Delegate, Spec
from agent_pm.linear_orchestrator.engine.types import Context
from agent_pm.selector import Answer, Failure, Kind, Question, Selector, SelectorError
from agent_pm.selector import State as SelectorState
from agent_pm.utils import OutcomeStatus

# Session's own config, read by name (no import of session) and merged with the local one.
SESSION_CONFIG = (("agent_pm.session", "config.toml"),)
SESSION_CLIENTS = "clients"
TRANSLATOR_ROLE = "choice-translator"
TRANSLATOR_TASK = "translate"
# The translate deliverable's JSON shape: its keys and each value's type.
TRANSLATION_FIELDS = {"title": str, "description": str, "status": str, "labels": list}
COMMENT_FIELDS = {"author": str, "created_at": str, "body": str}
MODEL_INSTRUCTIONS = (
    "Score how capable a model this issue's work needs: the lowest level that can still complete "
    "it to the standard it sets. The levels run from the least to the most capable model."
)
EFFORT_INSTRUCTIONS_NO_MODEL = (
    "Score the reasoning effort the run's model needs to complete this issue's work to the "
    "standard it sets: the lowest level that suffices."
)
EFFORT_INSTRUCTIONS = (
    "The run uses the model {model}. Score the reasoning effort that model needs to complete this "
    "issue's work to the standard it sets: the lowest level that suffices."
)
# A level's description by its rank, lowest first: (first, between, last); the Selector puts the
# option's name before it.
MODEL_RANKS = (
    "the least capable and least costly",
    "more capable and more costly than the levels before it",
    "the most capable and most costly",
)
EFFORT_RANKS = (
    "the least reasoning: fastest and cheapest",
    "more reasoning than the levels before it",
    "the most reasoning: slowest and costliest",
)


class SelectionFailure(StrEnum):
    TRANSLATE = "translate"
    SELECT = "select"
    INVALID = "invalid"
    LOW_CONFIDENCE = "low-confidence"


class SelectionError(Exception):
    """A failed chain step; str() is its code only; selector_reason: the Selector's code when its
    failure was the cause.
    """

    def __init__(self, reason: SelectionFailure, selector_reason: Failure | None = None):
        super().__init__(reason.value)
        self.reason = reason
        self.selector_reason = selector_reason


class Choice(ABC):
    @abstractmethod
    def apply(self, context: Context) -> Context: ...

    @abstractmethod
    def record(self) -> dict[str, object]:
        """Its runs.jsonl start-row fields, read back on resume."""


C = TypeVar("C", bound=Choice)


class Resolver(ABC, Generic[C]):
    """Lists its own options from config, given the context; callers pass it none."""

    @abstractmethod
    def choose(self, state: SelectorState, context: Context) -> C: ...

    @abstractmethod
    def fallback(self, context: Context) -> C:
        """Today's choice, without a Selector."""


@dataclass(frozen=True)
class ModelChoice(Choice):
    tier: int | None

    def apply(self, context: Context) -> Context:
        return replace(context, tier=self.tier)

    def record(self) -> dict[str, object]:
        return {} if self.tier is None else {"tier": self.tier}


@dataclass(frozen=True)
class EffortChoice(Choice):
    effort: str | None

    def apply(self, context: Context) -> Context:
        return replace(context, effort=self.effort)

    def record(self) -> dict[str, object]:
        return {} if self.effort is None else {"effort": self.effort}


class ModelResolver(Resolver[ModelChoice]):
    """Levels: the context client's distinct models in session's merged config, weakest (highest
    tier) first; a pick is its model's lowest tier. fallback: [task_tiers]'s tier for the task.
    """

    def __init__(self, selector: Selector, *, task_tiers: Mapping[str, int], min_confidence: float):
        self.selector = selector
        self.task_tiers = task_tiers
        self.min_confidence = min_confidence

    def choose(self, state: SelectorState, context: Context) -> ModelChoice:
        _check(state)
        tiers, _ = _client_table(context.client)
        lowest: dict[str, int] = {}
        for tier, model in tiers.items():
            lowest.setdefault(model, tier)
        weakest_first = list(reversed(lowest.items()))
        options = {
            model: f"tier {tier}, {_rank(i, len(weakest_first), MODEL_RANKS)}"
            for i, (model, tier) in enumerate(weakest_first)
        }
        pick = _select(self.selector, state, MODEL_INSTRUCTIONS, options, self.min_confidence)
        return ModelChoice(lowest[pick])

    def fallback(self, context: Context) -> ModelChoice:
        return ModelChoice(self.task_tiers.get(context.task or ""))


class EffortResolver(Resolver[EffortChoice]):
    """Levels: the context client's efforts in session's merged config, in config order (least
    first); the question names the model its tiers map the context's tier to. fallback: the role's
    effort.
    """

    def __init__(self, selector: Selector, *, min_confidence: float):
        self.selector = selector
        self.min_confidence = min_confidence

    def choose(self, state: SelectorState, context: Context) -> EffortChoice:
        _check(state)
        tiers, efforts = _client_table(context.client)
        model = None if context.tier is None else tiers.get(context.tier)
        instructions = (
            EFFORT_INSTRUCTIONS_NO_MODEL
            if model is None
            else EFFORT_INSTRUCTIONS.format(model=model)
        )
        options = {e: _rank(i, len(efforts), EFFORT_RANKS) for i, e in enumerate(efforts)}
        return EffortChoice(
            _select(self.selector, state, instructions, options, self.min_confidence)
        )

    def fallback(self, context: Context) -> EffortChoice:
        return EffortChoice(None)


def translate(
    issue: str, workdir: str, *, provider: type[Delegate], overlays: Sequence[Mapping] = ()
) -> SelectorState:
    """The state for issue (`TASK-123`), parsed from the JSON deliverable of one headless run,
    through provider (as LinearRun creates its runs), of the choice-translator role's task, whose
    config sets its client and tier: fields title, description, status and labels; droppable its
    comments, oldest first by created_at. A deliverable off that shape: INVALID.
    """
    if not issue.strip():
        raise ValueError("issue: want an issue identifier")
    try:
        result = provider.create(
            TRANSLATOR_ROLE, Spec(issue, workdir, task=TRANSLATOR_TASK), overlays=overlays
        ).run()
    except Exception:  # noqa: BLE001  # A failed translation run falls back to today's choices.
        raise SelectionError(SelectionFailure.TRANSLATE) from None
    outcome = result.outcome
    if outcome is None or outcome.status != OutcomeStatus.DONE:
        raise SelectionError(SelectionFailure.TRANSLATE)
    try:
        translation = json.loads(outcome.deliverable)
    except (ValueError, RecursionError):
        raise SelectionError(SelectionFailure.INVALID) from None
    if not _translation(translation):
        raise SelectionError(SelectionFailure.INVALID)
    comments = sorted(translation.pop("comments"), key=lambda c: c["created_at"])
    return SelectorState(translation, droppable=("comments", tuple(comments)))


def _translation(value: object) -> bool:
    """Whether value is the translate deliverable's exact shape."""
    if not isinstance(value, dict) or set(value) != {*TRANSLATION_FIELDS, "comments"}:
        return False
    if not all(isinstance(value[k], kind) for k, kind in TRANSLATION_FIELDS.items()):
        return False
    comments = value["comments"]
    return (
        all(isinstance(label, str) for label in value["labels"])
        and isinstance(comments, list)
        and all(
            isinstance(c, dict)
            and set(c) == set(COMMENT_FIELDS)
            and all(isinstance(c[k], kind) for k, kind in COMMENT_FIELDS.items())
            for c in comments
        )
    )


def _check(state):
    if not isinstance(state, SelectorState) or not state.fields:
        raise ValueError("state: want a State with fields")


def _client_table(client: str) -> tuple[dict[int, str], tuple[str, ...]]:
    """client's tiers (tier → model, in tier order) and effort names from session's merged config;
    a bad, missing or empty table: INVALID.
    """
    try:
        table = utils.read_resources(SESSION_CONFIG)[SESSION_CLIENTS][client]
        tiers, efforts = table["tiers"], table["efforts"]
        if not isinstance(tiers, Mapping) or not isinstance(efforts, Mapping):
            raise TypeError
        models = {int(tier): tiers[tier] for tier in sorted(tiers, key=int)}
    except (utils.ConfigFileError, LookupError, TypeError, ValueError):
        raise SelectionError(SelectionFailure.INVALID) from None
    if (
        not models
        or not efforts
        or not all(isinstance(m, str) and m.strip() for m in models.values())
    ):
        raise SelectionError(SelectionFailure.INVALID)
    return models, tuple(efforts)


def _rank(index: int, count: int, ranks: tuple[str, str, str]) -> str:
    """The description of the option at index among count: the first's, the last's or between's."""
    first, between, last = ranks
    return first if index == 0 else last if index == count - 1 else between


def _select(
    selector: Selector,
    state: SelectorState,
    instructions: str,
    levels: Mapping[str, str],
    min_confidence: float,
) -> str:
    """The pick of a score question on levels (a choice for a single one, which needs no call); a
    question off the Selector's limits (more than 10 levels): INVALID; an answer's confidence
    below min_confidence: LOW_CONFIDENCE.
    """
    try:
        question = Question(instructions, levels, Kind.SCORE if len(levels) > 1 else Kind.CHOICE)
    except ValueError:
        raise SelectionError(SelectionFailure.INVALID) from None
    try:
        answer = selector.select(state, question)
    except SelectorError as error:
        raise SelectionError(SelectionFailure.SELECT, error.reason) from None
    if not isinstance(answer, Answer) or answer.pick not in question.options:
        raise SelectionError(SelectionFailure.INVALID)
    if answer.confidence is not None and answer.confidence < min_confidence:
        raise SelectionError(SelectionFailure.LOW_CONFIDENCE)
    return answer.pick
