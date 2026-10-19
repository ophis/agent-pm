"""The Resolver chain the router runs before a new run: [selection] resolvers, in order, asking the
configured Selector. Translate the issue once, then each Step chooses its Choice from the state,
or takes its fallback without a state or failing; each Choice applies to the Context and records
its start-row fields. No resolvers: no chain. Logs carry only step names, reason codes and
records, never the issue, the state or an error's text.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agent_pm.delegate import Delegate
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.selection import (
    EffortResolver,
    ModelResolver,
    Resolver,
    SelectionError,
    translate,
)
from agent_pm.linear_orchestrator.engine.types import Config, Context, ResolverName
from agent_pm.selector import Selector


@dataclass(frozen=True)
class Step:
    name: ResolverName  # its [selection] resolvers name and log name
    resolver: Resolver


RESOLVERS: Mapping[ResolverName, Callable[[Config, Selector], Resolver]] = {
    ResolverName.MODEL: lambda cfg, selector: ModelResolver(selector, task_tiers=cfg.task_tiers),
    ResolverName.EFFORT: lambda _cfg, selector: EffortResolver(selector),
}


def steps(cfg: Config, selector: Selector | None = None) -> tuple[Step, ...]:
    """The [selection] resolvers' Steps asking selector, else a new configured Selector; none
    without resolvers.
    """
    names = cfg.selection.resolvers
    if not names:
        return ()
    if selector is None:
        configured = cfg.selection.selector
        selector = None if configured is None else configured()
        if not isinstance(selector, Selector):
            raise ValueError("[selection] resolvers need a Selector")  # config.load refuses it
    return tuple(Step(name, RESOLVERS[name](cfg, selector)) for name in names)


def resolve(
    issue: str,
    workdir: str,
    context: Context,
    chain: Sequence[Step],
    *,
    provider: type[Delegate],
    overlays: Sequence[Mapping],
) -> tuple[Context, dict[str, object]]:
    """The final Context and every step's records; no chain: context as given, no translate, no
    log. ValueError: two steps record the same field.
    """
    if not chain:
        return context, {}
    state = None
    try:
        state = translate(issue, workdir, provider=provider, overlays=overlays)
    except SelectionError as error:
        log("router", "choice", issue, step="translate", by="fallback", reason=error.reason)
    records: dict[str, object] = {}
    for step in chain:
        choice, reason, selector = None, None, None
        if state is None:
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
