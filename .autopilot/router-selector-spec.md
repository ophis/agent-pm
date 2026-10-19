# Spec: TASK-742 the selector interface, issue translation, the resolver chain and its tier resolver

Requirement source: the user's 补充要求 第 1 轮 on TASK-742 (2026-10-19, as corrected) and the user's decisions of the same day, recorded on the issue; it overrides the TASK-511 PRD and every earlier version of this spec. In order:

1. **Selector**: state text and a list of choices in, one decision API call, the chosen option out; abstract, Jev and Kev being separate implementations (later).
2. **Translation**: one function, `translate(issue, …) -> str`, no class: a ticket number in, the state text out (no choices), by running a new delegate role plus task, "choice translator", on Claude Haiku through delegate's existing public `Delegate`/`Spec` interface the way `LinearRun` does (`linear-orchestrator/src/agent_pm/linear_orchestrator/engine/linear_run.py:40-56`): headless, the ticket as input, the state read from its outcome; never session directly. Tests inject a fake Delegate provider (as `LinearRun`'s `delegate_cls`). One translation per ticket serves every resolver; a new kind of choice never changes it.
3. **Resolver chain**: the router runs resolvers in a fixed order. A `Resolver[C]` (renamed from Chooser so it does not read as a synonym of Selector) is abstract with `choose(state, context) -> C` and `fallback(context) -> C` (today's behaviour, for a step switched off or a failed `Selector`); a `Choice` is abstract with `apply(context) -> context` (writes itself into the context and the run's config) and `record() -> dict` (for runs.jsonl, used on resume). Each concrete resolver produces its concrete choice (factory-method style); each lists its own options from config given the context and passes them to the `Selector`; callers never pass options to a resolver. This issue: `TierResolver` → `TierChoice(tier, effort)`; later pairs `ClientResolver` → `ClientChoice(client)`, `RoleResolver` → `RoleChoice(role, task)`, which slot in earlier in the chain; later steps never change. The router loop is generic: for each resolver in the chain, choice = choose (or fallback), context = choice.apply(context), record choice.record(). Every step is optional: a switched-off step, or one whose `Selector` fails (error, timeout, no-key, invalid), falls back to today's behaviour (tier from `[task_tiers]`, client from the Agent label, role from routing), still fills its context entry, and the router logs only the reason code. In this issue the router fills role, task and client from the issue (client from its Agent label).
4. **Tier options**: the tier resolver reads the tier table from session's own config (`[clients.<client>.tiers]` and `efforts`), the single source, as the merged config: the packaged session `config.toml` with `~/.agent-pm/config.local.toml`'s overrides, via utils config_file, never the packaged resource alone; no copy of the tiers in the orchestrator config.
5. Dropped: model attribute table, usage, pin, ranking and exclusion details, multiple input kinds.

## Goal

TASK-511 wants the router to decide how a run launches from the ticket: first its tier and effort, later its client, role and task. This issue ships what that goes through: the abstract `Selector` with a fake; `translate`, one delegate run per ticket that yields the state; the chain's `Context`, abstract `Choice` and `Resolver[C]`; and the one resolver this issue builds, `TierResolver`, which lists the context client's tiers and efforts from session's merged config, asks a `Selector`, and falls back to `[task_tiers]`. No Jev or Kev call, delegate role, router change or config switch ships here.

## Scope

| In | Out (later issues) |
| --- | --- |
| New project `selector/`, package `agent_pm.selector`: `Selector` ABC, `SelectorConfig`, `Backend`, `Failure`, `SelectorError`; `FakeSelector`, `SelectorContract` | `JevSelector` and `KevSelector` and their HTTP calls |
| `linear_orchestrator.engine.selection`: `translate`; `Context`; abstract `Choice` and `Resolver[C]`; `TierChoice`, `TierResolver`; `SelectionError`; `FakeResolver`, `ResolverContract`, `TierChoiceContract` | The `choice-translator` role, its task and its config (client, Haiku tier); the router loop, `Context` filling, the per-step on/off keys, logging and resume from `record()`; `ClientResolver`/`ClientChoice`, `RoleResolver`/`RoleChoice` |
| Import contracts, guard data and repo wiring for the new project; vulture entries; docs | Model attribute table, usage, pin, ranking, exclusion, multiple input kinds (dropped); `agent-pm-select` CLI, `select-model` skill |

Constraints:
- No session or delegate change; `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main` (FR-30).
- No compatibility code; no stub functions: `Selector`, `Choice` and `Resolver` are ABCs.
- No `tier` or `linear` (case-insensitive) under `selector/`, no client name in its `src` (FR-26, `client_names`); no client name in linear_orchestrator's source (`orchestrator_text`).
- Tests never call the keychain, a network or a real delegate run.

## Flow

```mermaid
sequenceDiagram
  participant R as router (later issue)
  participant T as translate
  participant D as Delegate (choice-translator role)
  participant V as TierResolver
  participant K as session config (merged)
  participant S as Selector (Jev or Kev)
  R->>T: translate(issue, workdir, provider)
  T->>D: create(role, Spec(issue, headless)).run()
  D-->>T: outcome.deliverable
  T-->>R: state
  Note over R: context role, task and client from routing and the Agent label
  R->>V: choose(state, context)
  V->>K: clients.client.tiers and efforts
  V->>S: select(state, options)
  S-->>V: one of options
  V-->>R: TierChoice(tier, effort)
  Note over R,V: step off or SelectionError, then fallback(context), log the reason code only
  Note over R: context = choice.apply(context), record choice.record()
```

The router loop (binding the router issue), for each enabled-or-not resolver in the chain's fixed order:

```python
for resolver in chain:                       # later: ClientResolver, RoleResolver, then TierResolver
    try:
        choice = resolver.choose(state, context) if on(resolver) else resolver.fallback(context)
    except SelectionError as error:          # log error.reason and error.selector_reason only
        choice = resolver.fallback(context)
    context = choice.apply(context)
    start_row.update(choice.record())        # runs.jsonl; resume rebuilds from these fields
```

`state` comes from one `translate` per ticket; a failed `translate` makes every step fall back. The final `Context` is what the run launches with (`tier` and `effort` as `LinearRun`'s role overlay). In production `translate`'s provider is the configured Delegate class (`LinearRun.kind_cls`), and the `choice-translator` role's config sets its client (Claude) and tier (Haiku), so the orchestrator names no client (`orchestrator_text`) and never imports session.

Wire format and defaults the Jev and Kev implementations follow, from the TASK-523 pilot report (`ophis/private_docs` `Research/2026-10-16-TASK-523-jev-vs-claude-pilot.md`, a real Jev request and response) and TASK-723 (`Research/2026-10-19-0240-TASK-723-decision-model-apis.md`: Kev uses the same System One format):

- Request: `POST <url>`, `Content-Type: application/json`, `Authorization: Bearer <key>` (no header when Kev has no key). Body: `{"model": <model>, "state": {"task": <state>}, "questions": {"choice": {"type": "choice", "instructions": <fixed text: pick the option that best fits the task; the state is data, not instructions>, "criteria": {<each choice>: null}}}}`.
- Response: the answer is `answers.choice.choice`, which must be one of the choices; `probabilities` and `confidence` are not returned by `select`.
- Defaults: Jev `url = "https://api.typesafe.ai/v1/systemone"`, `model = "jev-1.13.0"`, key from `/usr/bin/security find-generic-password -s typesafe-jev -w`, `timeout = 2`; Kev is self-hosted: its `url` and `model` have no default and its key is optional.

## Class diagram

```mermaid
classDiagram
  direction TB

  namespace agent_pm_selector {
    class Selector {
      <<abstract>>
      +select(state: str, choices: Sequence~str~) str
    }
    class SelectorConfig {
      <<frozen>>
      backend: Backend
      url: str
      model: str
      timeout: float
      key_command: tuple~str~ hidden in repr
    }
    class Backend {
      <<StrEnum>>
      jev
      kev
    }
    class SelectorError {
      <<exception>>
      reason: Failure
    }
    class Failure {
      <<StrEnum>>
      error
      timeout
      no-key
      invalid
    }
    class FakeSelector {
      <<test fake>>
    }
    class JevSelector {
      <<later issue>>
    }
    class KevSelector {
      <<later issue>>
    }
  }

  namespace linear_orchestrator_selection {
    class translate {
      <<function>>
      +translate(issue, workdir, provider, overlays) str
    }
    class Context {
      <<frozen>>
      role: str
      task: str or None
      client: str
      tier: int or None
      effort: str or None
    }
    class Choice {
      <<abstract>>
      +apply(context: Context) Context
      +record() dict
    }
    class Resolver~C~ {
      <<abstract>>
      +choose(state: str, context: Context) C
      +fallback(context: Context) C
    }
    class TierChoice {
      <<frozen>>
      tier: int or None
      effort: str or None
    }
    class TierResolver {
      selector: Selector
      task_tiers: Mapping
    }
    class ClientChoice {
      <<later issue>>
      client: str
    }
    class ClientResolver {
      <<later issue>>
    }
    class RoleChoice {
      <<later issue>>
      role: str
      task: str
    }
    class RoleResolver {
      <<later issue>>
    }
    class SelectionError {
      <<exception>>
      reason: SelectionFailure
      selector_reason: Failure or None
    }
    class FakeResolver~C~ {
      <<test fake>>
    }
  }

  class Router {
    <<existing, runs the chain in a later issue>>
  }
  class Delegate {
    <<agent_pm.delegate, as LinearRun uses it>>
  }
  class SessionConfig {
    <<session config.toml plus config.local.toml>>
  }
  class DecisionAPI {
    <<Jev or Kev, POST /v1/systemone>>
  }

  Selector <|-- FakeSelector
  Selector <|-- JevSelector
  Selector <|-- KevSelector
  Choice <|-- TierChoice
  Choice <|-- ClientChoice
  Choice <|-- RoleChoice
  Resolver <|-- TierResolver
  Resolver <|-- ClientResolver
  Resolver <|-- RoleResolver
  Resolver <|-- FakeResolver
  TierResolver ..> TierChoice : creates
  ClientResolver ..> ClientChoice : creates
  RoleResolver ..> RoleChoice : creates
  Choice ..> Context : apply
  SelectorConfig --> Backend
  SelectorError --> Failure
  SelectionError --> Failure

  Router ..> translate : once per issue
  Router ..> Resolver : choose or fallback, in chain order
  Router ..> Choice : apply, record
  translate ..> Delegate : create, run
  TierResolver ..> SessionConfig : the context client's tiers and efforts
  TierResolver ..> Selector : select(state, its options)
  JevSelector ..> DecisionAPI : one call
  KevSelector ..> DecisionAPI : one call
```

Dashed arrow: a call, use or creation; `<|--`: implements. Classes marked later issue are not built here; neither are the router loop, the role or the Jev and Kev calls.

## Acceptance criteria

- AC1 (selector types): `agent_pm.selector.Selector` is an ABC whose only abstract method is `select(state: str, choices: Sequence[str]) -> str`; `SelectorConfig(backend, url, model, timeout, key_command=())` is frozen and its `repr` holds none of `key_command`'s values; `Backend` values are exactly `jev`, `kev`; `SelectorError(reason)` has `str()` exactly its code; `Failure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC2 (selector fake): `FakeSelector` passes `SelectorContract` (result one of `choices`; one choice → it; empty, blank or repeated choices → `ValueError`; same input → same result); `FakeSelector(picks={"s": "b"})` returns `"b"` for state `"s"`, else the first choice; a scripted pick outside the choices → `SelectorError(INVALID)`; `FakeSelector(error=e)` raises `e`; `calls` records each `(state, choices)`.
- AC3 (translate): `translate("TASK-1", workdir, provider=P, overlays=(core,))` calls `P.create("choice-translator", Spec("TASK-1", workdir, task="translate"), overlays=(core,))` once (headless, no client, no resume) and `run()` on it, and returns its outcome's `deliverable`, stripped.
- AC4 (translate failures): a blank issue → `ValueError` with no `create`; `create` or `run` raising → `SelectionError(TRANSLATE)` raised `from None`; no outcome, or one whose status is not `done` → `SelectionError(TRANSLATE)`; a blank deliverable → `SelectionError(INVALID)`; no message holds issue text, the deliverable or the exception's text.
- AC5 (chain types): `Resolver[C]` is an ABC generic over `C` (bound to `Choice`) whose abstract methods are exactly `choose(state, context) -> C` and `fallback(context) -> C`; `Choice` is an ABC whose abstract methods are exactly `apply(context) -> Context` and `record() -> dict`; `Context(role, task, client, tier=None, effort=None)` is frozen; `SelectionError(reason, selector_reason=None)` has `str()` exactly its code; `SelectionFailure` values are exactly `translate`, `select`, `invalid`.
- AC6 (tier choice): `TierChoice(3, "low").apply(context)` returns the context with `tier=3`, `effort="low"`, the rest unchanged; `record()` returns `{"tier": 3, "effort": "low"}`, leaving out a `None` field.
- AC7 (tier options): `TierResolver(selector, task_tiers={}).choose(state, Context("engineer", "build", "claude"))` calls `selector.select` once with `state` unchanged and one option per (model, effort): models from `clients.claude.tiers` of session's merged config in tier order, each once (its lowest tier), efforts from `clients.claude.efforts` in config order, each option `<model> / <effort>`; the pick maps back to `TierChoice(<that model's lowest tier>, <effort>)`.
- AC8 (local override): with `~/.agent-pm/config.local.toml` (the test's temp HOME) setting `[session.clients.opencode.tiers]` to two models, `TierResolver` for client `opencode` offers exactly those; the tables equal session's own merged parse.
- AC9 (tier failures and fallback): a blank state → `ValueError`; a client with no `tiers` or `efforts` → `SelectionError(INVALID)`; the `Selector` raising `SelectorError(code)` → `SelectionError(SELECT, code)` raised `from None`; a pick not among the options → `SelectionError(INVALID)`; `fallback(context)` → `TierChoice(task_tiers[context.task], None)`, or `TierChoice(None, None)` when the task has no entry or is `None` (the role's own tier and effort, as today).
- AC10 (resolver fake and contracts): `TierResolver` and `FakeResolver[TierChoice]` pass `ResolverContract` (blank state → `ValueError`; same state and context → same choice; `choose` and `fallback` return a `Choice` whose `apply` returns a `Context` and `record` a `dict`) and `TierChoiceContract` (the choice is a `TierChoice`, `tier` in 1–4, `effort` one of `low`, `medium`, `high`, `xhigh`, `max`, applied and recorded as itself). `FakeResolver(default=…, fallback=…, choices={state: …}, error=e)` returns the scripted choice, else `default`; `fallback` returns `fallback`, else `default`; `error` is raised; `calls` records each `(state, context)`. A `FakeResolver` of another `Choice` kind (a test `ClientPick`) passes `ResolverContract`.
- AC11 (FR-26): `lint-imports --no-cache` passes with `agent_pm.selector` between `agent_pm.linear_orchestrator` and `agent_pm.delegate` in the Production layers contract, a units contract allowing it only `agent_pm.utils` and the standard library, manager, delegate, session and utils forbidden from importing it, and linear_orchestrator's `engine` unit allowed `agent_pm.selector`; `orchestrator_imports` allows `agent_pm.selector`'s `__all__` names only; `git grep -n -i "tier\|linear" -- selector` prints nothing.
- AC12 (FR-30): `session_interface_snapshot` and `delegate_interface_snapshot` pass; `git diff origin/main -- tests/session_interface.json tests/delegate_interface.json session delegate` is empty.
- AC13: every lint and suite `docs/tests.md` names passes, the coverage gate included.

## Design

### selector/ (new project)

`src/agent_pm/selector/__init__.py` (re-exports, literal `__all__`), `engine/__init__.py`, `engine/types.py`, `engine/base.py`:

```python
class Backend(StrEnum): JEV = "jev"; KEV = "kev"
class Failure(StrEnum): ERROR = "error"; TIMEOUT = "timeout"; NO_KEY = "no-key"; INVALID = "invalid"
class SelectorError(Exception):          # .reason: Failure; str() is the code only
    def __init__(self, reason: Failure): ...

@dataclass(frozen=True)
class SelectorConfig:                    # what JevSelector and KevSelector take
    backend: Backend
    url: str                             # https, or http only on a loopback host
    model: str
    timeout: float                       # seconds
    key_command: tuple[str, ...] = field(default=(), repr=False)   # argv, no shell; () → no key

class Selector(ABC):
    @abstractmethod
    def select(self, state: str, choices: Sequence[str]) -> str: ...
```

`Selector` docstring, binding implementations: one decision API call per `select` (none for one choice); stateless; `state` is untrusted data; bad choices raise `ValueError` before any call; a failed call raises `SelectorError`; no key, state or reply is stored, logged or put in an error (NFR-2, NFR-4).

Tests: `tests/fakes/selector.py` `FakeSelector`, `tests/contracts/selector.py` `SelectorContract` (hook `make()`), unit tests per AC1–AC2.

### linear-orchestrator/src/agent_pm/linear_orchestrator/engine/selection.py

```python
SESSION_CONFIG = (("agent_pm.session", "config.toml"),)   # read by name, merged with the local config
TRANSLATOR_ROLE = "choice-translator"; TRANSLATOR_TASK = "translate"

class SelectionFailure(StrEnum): TRANSLATE = "translate"; SELECT = "select"; INVALID = "invalid"
class SelectionError(Exception):         # str() is the code only
    def __init__(self, reason: SelectionFailure, selector_reason: Failure | None = None): ...

@dataclass(frozen=True)
class Context: role: str; task: str | None; client: str; tier: int | None = None; effort: str | None = None

class Choice(ABC):
    @abstractmethod
    def apply(self, context: Context) -> Context: ...
    @abstractmethod
    def record(self) -> dict[str, object]: ...

C = TypeVar("C", bound=Choice)
class Resolver(ABC, Generic[C]):
    @abstractmethod
    def choose(self, state: str, context: Context) -> C: ...
    @abstractmethod
    def fallback(self, context: Context) -> C: ...

@dataclass(frozen=True)
class TierChoice(Choice): tier: int | None; effort: str | None    # None: the role's own

class TierResolver(Resolver[TierChoice]):
    def __init__(self, selector: Selector, *, task_tiers: Mapping[str, int]): ...   # the router passes cfg.task_tiers

def translate(issue: str, workdir: str, *, provider: type[Delegate], overlays: Sequence[Mapping] = ()) -> str: ...
```

`TierResolver.choose` reads `clients.<context.client>.tiers` and `.efforts` with `utils.read_resources(SESSION_CONFIG)` (merged with `~/.agent-pm/config.local.toml`), one read per `choose`.

Tests: `linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py` (`FakeResolver`, `ResolverContract` with hooks `make()` and `context()`, `TierChoiceContract`); `orchestrator_tests/unit/engine/selection_test.py` per AC3–AC10: `translate` with a `FakeDelegate` subclass as provider (`delegate/tests/fakes/delegate.py`), `TierResolver` with `FakeSelector` and local overrides in the test's temp HOME, a `ClientPick` test choice.

### Repository wiring

`tests/guards.py` `LEVELS` gets `("selector",)` between delegate and linear-orchestrator; `tests/guard_data/selector.py` (`OBJECT_EXPORT_MODULES`, `SELECTOR_SRC`); `pyproject.toml` pyright, vulture, `root_packages`, the AC11 contracts, a `selector.tests` ownership contract and lower test-ownership contracts; `tests/guard_checks/imports.py` `orchestrator_imports` allows `agent_pm.selector`'s `__all__`; `tests/guard_checks/ownership.py` `orchestrator_text` honours `TOOL_STRINGS_ALLOWED` in `tests/guard_data/linear_orchestrator.py`, whose one entry lets `engine/selection.py` name session's `clients` table (D10); `NOQA_ALLOWED` gets `translate`'s `BLE001`; repo tests that list the projects (`tests/unit/case_test.py`, `guards_test.py`, `guard_checks/placement_test.py`); PYTHONPATH and src lists in `.github/workflows/test.yml`, `docs/tests.md`, `bin/agent-pm-linear`; `.github/vulture_whitelist.py` entries for names no `src` code uses yet, naming TASK-511.

### Docs

`docs/architecture.md`: six layers, the Flow and router loop. `CONTEXT.md`: terms **Selector**, **Resolver**, **Choice**, **Resolver chain**. `AGENTS.md` Import rules: the layer order.

## Verification

| AC | Test or check |
| --- | --- |
| AC1 | `selector/tests/unit/engine/types_test.py` |
| AC2 | `selector/tests/unit/fakes/selector_test.py` |
| AC3 | `linear-orchestrator/tests/orchestrator_tests/unit/engine/selection_test.py` `Translating` |
| AC4 | the same, `Translating` failure cases |
| AC5 | the same, `Types` |
| AC6 | the same, `Types` tier choice case |
| AC7 | the same, `TierOptions` |
| AC8 | the same, `LocalOverride` |
| AC9 | the same, `TierFailures`, `TierFallback` |
| AC10 | the same, the contract classes and `FakeScript` |
| AC11 | `lint-imports --no-cache`; `python3 -m tests.checks`; `tests/unit/guard_checks/imports_test.py`; `git grep` |
| AC12 | `python3 -m tests.checks`; `git diff` |
| AC13 | the lint commands and suites of `docs/tests.md`; CI |

## Decisions

- D1: `translate`, `Context`, `Choice`, `Resolver` and `TierResolver` live in linear_orchestrator: the ticket, the tier and the client are outside FR-26's generic concepts; only the selector is generic.
- D2: Choices passed to a `Selector` are plain strings and the answer one of them.
- D3: `Selector`, `Choice` and `Resolver` are ABCs with no stub functions; `TierChoice`, `TierResolver` and `translate` are concrete.
- D4: `TierChoice.effort` and `Context.effort` are `str` so the orchestrator does not import session's `Effort`; `TierChoiceContract` checks the values.
- D5: The router loop, the on/off config keys, `Context` filling, logging and resume from `record()` come with the router issue; this issue fixes their contract (Flow).
- D6: The choice translator's reply is the state text itself; `translate` reads `Outcome.deliverable`.
- D7: `Resolver` is generic over its `Choice` (`Generic[C]`, `C` bound to `Choice`, Python 3.11), each concrete resolver producing its concrete choice.
- D8: The Jev and Kev implementations send `state` as `{"task": <state>}`, each choice with a `null` description, under one fixed question id `choice`.
- D9: `Resolver.choose` takes the state, not the ticket: one translation per ticket serves every step.
- D10: Tier options come from session's own config, merged: `utils.read_resources(SESSION_CONFIG)` with its default `local`, a resource read by name, not an import of session (FR-31, FR-30); never the packaged resource alone, no tier copy in the orchestrator config. Naming session's `clients` table is one `TOOL_STRINGS_ALLOWED` exception to `orchestrator_text`.
- D11: Options are one per (model, effort): tiers naming one model (OpenCode's four) give it once, and a pick maps back to its lowest tier; the label `<model> / <effort>` names what the decision API can judge.
- D12: `Context` carries role, task, client, tier and effort, the run's config so far; a `None` tier or effort means the role's configured one, so `TierChoice(None, None)` is today's behaviour for a task without `[task_tiers]`.
- D13: Fallback is the router's: a resolver raises `SelectionError` (with `selector_reason`, the `Selector`'s code, for the log line) and the router calls `fallback`; resolvers never fall back silently.
- D14: `translate` sets no client or tier: the `choice-translator` role's config sets Claude and its Haiku tier (the role's issue), since `orchestrator_text` forbids client names in the orchestrator; it passes the router's overlays (`cfg.core`) as `LinearRun` does.
- D15: The method names stay `choose` and `fallback` as the user gave them; only the class names became `Resolver`, `TierResolver`.
- Left to the implementer: docstring wording, test names.
