"""The router's choice chain. translate turns an issue into the state once; each step is a Resolver
paired with its Choice: choose picks one for the state given the Context so far, among options it
reads from config, by asking a Selector; fallback gives today's choice, for a step switched off or
a SelectionError. The router applies each Choice to the Context in a fixed order and records it for
resume, logging only reason codes: issue text, the state and replies are untrusted data.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Generic, TypeVar

import agent_pm.utils as utils
from agent_pm.delegate import Delegate, Spec
from agent_pm.linear_orchestrator.engine.types import Context
from agent_pm.selector import Failure, Question, Selector, SelectorError
from agent_pm.selector import State as SelectorState
from agent_pm.utils import OutcomeStatus

# Session's own config, read by name (no import of session) and merged with the local one.
SESSION_CONFIG = (("agent_pm.session", "config.toml"),)
SESSION_CLIENTS = "clients"
TRANSLATOR_ROLE = "choice-translator"
TRANSLATOR_TASK = "translate"


class SelectionFailure(StrEnum):
    TRANSLATE = "translate"
    SELECT = "select"
    INVALID = "invalid"


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
    def choose(self, state: str, context: Context) -> C: ...

    @abstractmethod
    def fallback(self, context: Context) -> C:
        """Today's choice, without a Selector."""


@dataclass(frozen=True)
class TierChoice(Choice):
    tier: int | None
    effort: str | None

    def apply(self, context: Context) -> Context:
        return replace(context, tier=self.tier, effort=self.effort)

    def record(self) -> dict[str, object]:
        return {k: v for k, v in (("tier", self.tier), ("effort", self.effort)) if v is not None}


class TierResolver(Resolver[TierChoice]):
    """Options: one per model and effort of the context client's tiers and efforts in session's
    merged config; a pick maps back to its model's lowest tier. fallback: [task_tiers]'s tier for
    the task, the role's effort.
    """

    def __init__(self, selector: Selector, *, task_tiers: Mapping[str, int]):
        self.selector = selector
        self.task_tiers = task_tiers

    def choose(self, state: str, context: Context) -> TierChoice:
        if not state.strip():
            raise ValueError("state: want a non-blank state")
        options = _tier_options(context.client)
        try:
            pick = self.selector.select(
                SelectorState({"text": state}),
                Question("Choose the option that best fits the state.", {o: o for o in options}),
            )
        except SelectorError as error:
            raise SelectionError(SelectionFailure.SELECT, error.reason) from None
        if pick not in options:
            raise SelectionError(SelectionFailure.INVALID)
        return options[pick]

    def fallback(self, context: Context) -> TierChoice:
        return TierChoice(self.task_tiers.get(context.task or ""), None)


def translate(
    issue: str, workdir: str, *, provider: type[Delegate], overlays: Sequence[Mapping] = ()
) -> str:
    """The state for issue (`TASK-123`): the deliverable of one headless run, through provider (as
    LinearRun creates its runs), of the choice-translator role's task, whose config sets its client
    and tier.
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
    state = outcome.deliverable.strip()
    if not state:
        raise SelectionError(SelectionFailure.INVALID)
    return state


def _tier_options(client: str) -> dict[str, TierChoice]:
    try:
        table = utils.read_resources(SESSION_CONFIG)[SESSION_CLIENTS][client]
        tiers, efforts = table["tiers"], table["efforts"]
        if not isinstance(tiers, Mapping) or not isinstance(efforts, Mapping):
            raise TypeError
        lowest = {}
        for tier in sorted(tiers, key=int):
            lowest.setdefault(tiers[tier], int(tier))
    except (utils.ConfigFileError, LookupError, TypeError, ValueError):
        raise SelectionError(SelectionFailure.INVALID) from None
    options = {
        f"{model} / {effort}": TierChoice(tier, effort)
        for model, tier in lowest.items()
        for effort in efforts
    }
    if not options:
        raise SelectionError(SelectionFailure.INVALID)
    return options
