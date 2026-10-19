"""The Resolver chain the router runs before a new run: translate the issue once, then each Step in
order chooses its Choice from the state, or takes its fallback when switched off, without a state
or failing; each Choice applies to the Context and records its start-row fields. Logs carry only
step names, reason codes and records, never the issue, the state or an error's text.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_pm.delegate import Delegate
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.selection import (
    Resolver,
    SelectionError,
    TierResolver,
    translate,
)
from agent_pm.linear_orchestrator.engine.types import Config, Context, SelectionStep
from agent_pm.selector import Failure, Selector, SelectorError


@dataclass(frozen=True)
class Step:
    name: str  # its [selection] key and log name
    resolver: Resolver
    on: bool


class NoSelector(Selector):
    """The Selector until a decision API is wired (Jev, Kev): every select fails with no-key."""

    def select(self, state, choices):
        raise SelectorError(Failure.NO_KEY)


def steps(cfg: Config, selector: Selector) -> tuple[Step, ...]:
    """The chain in its fixed order: today only the tier step."""
    tier = TierResolver(selector, task_tiers=cfg.task_tiers)
    return (Step(SelectionStep.TIER.value, tier, cfg.selection[SelectionStep.TIER]),)


def resolve(
    issue: str,
    workdir: str,
    context: Context,
    chain: Sequence[Step],
    *,
    translate_on: bool,
    provider: type[Delegate],
    overlays: Sequence[Mapping],
) -> tuple[Context, dict[str, object]]:
    """The final Context and every step's records; a state only when translate and a step are on.
    ValueError: two steps record the same field.
    """
    logged = translate_on or any(step.on for step in chain)
    state = None
    if translate_on and any(step.on for step in chain):
        try:
            state = translate(issue, workdir, provider=provider, overlays=overlays)
        except SelectionError as error:
            log("router", "choice", issue, step="translate", by="fallback", reason=error.reason)
    records: dict[str, object] = {}
    for step in chain:
        choice, reason, selector = None, None, None
        if not step.on:
            reason = "off"
        elif state is None:
            reason = "no-state"
        else:
            try:
                choice = step.resolver.choose(state, context)
            except SelectionError as error:
                reason, selector = error.reason, error.selector_reason
            except Exception:  # noqa: BLE001  # A resolver breaking its contract still falls back.
                reason = "error"
        by = "fallback" if choice is None else "selector"
        if choice is None:
            choice = step.resolver.fallback(context)
        context = choice.apply(context)
        record = choice.record()
        if (twice := next((k for k in record if k in records), None)) is not None:
            raise ValueError(f"choice record field {twice} recorded twice")
        records.update(record)
        if logged:
            log(
                "router",
                "choice",
                issue,
                step=step.name,
                by=by,
                reason=reason,
                selector=selector,
                choice=record,
            )
    return context, records
