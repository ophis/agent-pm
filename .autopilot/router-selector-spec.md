# Spec: TASK-742 the selector interface, the choice translator, the chooser chain and its tier chooser

Requirement source: the user's 补充要求 第 1 轮 on TASK-742 (2026-10-19, as corrected) and the user's interface and chain decisions of the same day, recorded on the issue, including: a switched-off step still fills its context entry with today's value, and a step whose `Selector` fails (error, timeout, no-key, invalid) falls back the same way, logging only the reason code. (1) Selector: state text and a list of choices in, one decision API call, the chosen option out; it stays abstract, Jev and Kev being separate implementations (later). (2) Choice translator: a ticket number in, a small model reads the ticket, the state text out (no choices); a new delegate role plus task, "choice translator", on Claude Haiku, called through delegate, never session; one concrete class, no abstract base, no fake class: tests inject a fake at its boundary, the delegate call; one translation per ticket serves every chooser, and a new kind of choice never changes it. (3) Chooser chain, approved by the user: the router runs choosers in a fixed order; each is `Chooser[T].choose(state, context) -> T`, the context holding the choices made so far (this issue: role, task and client filled by the router from the issue, client from its Agent label); each chooser lists its own options from config given the context (the tier chooser: the context client's tiers), then calls the `Selector`; later choosers (client, role and task) slot in earlier in the chain and write their pick into the context; later steps never change; every step is optional: each chooser can be switched off in config, and a switched-off step falls back to today's behaviour (tier from `[task_tiers]`, client from the Agent label, role from routing); this issue builds only the tier chooser. (4) The tier chooser reads the tier table from session's own config (`[clients.<client>.tiers]` and `efforts`), the single source, as the merged config: the packaged session `config.toml` with `~/.agent-pm/config.local.toml`'s overrides, via utils config_file, never the packaged resource alone; no copy of the tiers in the orchestrator config. Dropped: model attribute table, usage, pin, ranking and exclusion details, multiple input kinds. It overrides the TASK-511 PRD and every earlier version of this spec.

## Goal

TASK-511 wants the router to choose how a run launches from the ticket: first its tier and effort, later its client, role and task. This issue ships the parts that choice goes through: the abstract `Selector` (one decision API call per question) with a fake; the concrete `Translator` (ticket → state, once per ticket); the chain's `Context` and generic `Chooser[T]`; and the one chooser this issue builds, `TierChooser`, which reads the context client's tiers and efforts from session's merged config and asks a `Selector`. No Jev or Kev call, delegate role, router change or config switch ships here; those are later issues.

## Scope

| In | Out (later issues) |
| --- | --- |
| New project `selector/`, package `agent_pm.selector`: `Selector` ABC, `SelectorConfig`, `Backend`, `Failure`, `SelectorError`; `FakeSelector`, `SelectorContract` | `JevSelector` and `KevSelector`, separate `Selector` implementations, and their HTTP calls |
| `linear_orchestrator.engine.selection`: concrete `Translator`; `Context`; generic `Chooser[T]` ABC; `TierChoice`; concrete `TierChooser`; `SelectionError`; `FakeChooser`, `ChooserContract`, `TierChoiceContract` | The `choice-translator` role, its task and the delegate call the `Translator` gets in production; the router running the chain, filling `Context`, the per-chooser on/off config keys and their fallbacks; the client, role and task choosers |
| Import contracts and repo wiring for the new project; vulture entries; docs | Model attribute table, usage, pin, ranking, exclusion, multiple input kinds (dropped); `agent-pm-select` CLI, `select-model` skill |

Constraints:
- No session or delegate change; `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main` (FR-30).
- No compatibility code; no stub functions: `Selector` and `Chooser` are ABCs.
- No `tier` or `linear` (case-insensitive) under `selector/`, no client name in its `src` (FR-26, `client_names`).
- Tests never call the keychain, a network or a real delegate run.

## Flow

```mermaid
sequenceDiagram
  participant R as router (later issue)
  participant T as Translator
  participant D as delegate call (choice-translator role)
  participant C as TierChooser
  participant K as session config (merged)
  participant S as Selector (Jev or Kev)
  R->>T: translate(ticket)
  T->>D: ask(ticket)
  D-->>T: the role's deliverable text
  T-->>R: state
  Note over R: context role and task from routing, client from the Agent label
  R->>C: choose(state, context)
  C->>K: clients.<context.client>.tiers, efforts
  C->>S: select(state, options)
  S-->>C: one of options
  C-->>R: TierChoice(tier, effort)
  Note over R,C: step off or SelectionError gives today's value, logs the reason code only
```

Chain convention (binding the router issue): the router translates each ticket once, then runs the enabled choosers in a fixed order, each with the same state and the `Context` so far; a later chooser kind (client, then role and task) runs earlier in the chain and returns a new `Context` carrying its pick, so a step's input never changes after it runs; a switched-off step, or no chooser at all, keeps today's behaviour (tier from `[task_tiers]`, client from the Agent label, role from routing) and still fills its `Context` entry with that value, never leaving it missing; a step whose chooser raises `SelectionError` (its `Selector` failed with `error`, `timeout`, `no-key` or `invalid`, or its translation or pick was invalid) falls back the same way as a switched-off step, and the router logs only the reason code (`SelectionError.reason`, plus `SelectorError.reason` when that was the cause), never the state, the ticket text or a reply. In this issue the chain is the tier step alone. In production the `Translator`'s `ask` runs the `choice-translator` role's task through `agent_pm.delegate` on Claude Haiku (the role's `client = "claude"`, `tier = 4`), never `agent_pm.session` (forbidden by the import contracts).

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
      picks: Mapping~str, str~
      error: SelectorError | None
      calls: list
    }
    class JevSelector {
      <<later issue>>
    }
    class KevSelector {
      <<later issue>>
    }
  }

  namespace linear_orchestrator_selection {
    class Translator {
      <<concrete>>
      ask: Callable~str, str~
      +translate(ticket: str) str
    }
    class Context {
      <<frozen>>
      role: str
      task: str | None
      client: str
    }
    class Chooser~T~ {
      <<abstract>>
      +choose(state: str, context: Context) T
    }
    class TierChooser {
      <<concrete>>
      selector: Selector
      +choose(state, context) TierChoice
    }
    class TierChoice {
      <<frozen>>
      tier: int
      effort: str
    }
    class SelectionError {
      <<exception>>
      reason: SelectionFailure
    }
    class SelectionFailure {
      <<StrEnum>>
      translate
      select
      invalid
    }
    class FakeChooser~T~ {
      <<test fake>>
      choices: Mapping~str, T~
      default: T
      error: Exception | None
      calls: list
    }
  }

  class Router {
    <<existing; runs the chain in a later issue>>
  }
  class ChoiceTranslatorRole {
    <<delegate role choice-translator, Claude Haiku>>
  }
  class SessionConfig {
    <<session config.toml + config.local.toml>>
    clients.client.tiers
    clients.client.efforts
  }
  class DecisionAPI {
    <<Jev or Kev, POST /v1/systemone>>
  }

  Selector <|-- FakeSelector
  Selector <|-- JevSelector
  Selector <|-- KevSelector
  Chooser <|-- TierChooser : T = TierChoice
  Chooser <|-- FakeChooser
  SelectorConfig --> Backend
  SelectorError --> Failure
  SelectionError --> SelectionFailure
  TierChooser ..> TierChoice : returns
  Chooser ..> Context : reads

  Router ..> Translator : translate(ticket), once
  Router ..> Context : fills role, task, client
  Router ..> Chooser : choose(state, context), in chain order
  Translator ..> ChoiceTranslatorRole : ask(ticket), via delegate
  TierChooser ..> SessionConfig : the context client's tiers, efforts
  TierChooser ..> Selector : select(state, options)
  JevSelector ..> DecisionAPI : one call
  KevSelector ..> DecisionAPI : one call
  JevSelector ..> SelectorConfig
  KevSelector ..> SelectorConfig
```

Dashed arrow: a call or use; `<|--`: implements. `JevSelector`, `KevSelector`, the role, the router running the chain (with its on/off switches) and the client, role and task choosers are later issues; the rest ships here. Each later chooser is a `Chooser[T]` with its own `T` and options, reads the same state, and writes its pick into the `Context`; the `Translator` never changes for them.

## Acceptance criteria

- AC1 (selector types): `agent_pm.selector.Selector` is an ABC whose only abstract method is `select(state: str, choices: Sequence[str]) -> str`; `SelectorConfig(backend, url, model, timeout, key_command=())` is frozen and its `repr` holds none of `key_command`'s values; `Backend` values are exactly `jev`, `kev`; `SelectorError(reason)` has `str()` exactly its code; `Failure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC2 (selector fake): `FakeSelector` passes `SelectorContract`: the result is one of `choices`; one choice → that choice; empty, blank or repeated choices → `ValueError`; same input → same result. `FakeSelector(picks={"s": "b"})` returns `"b"` for state `"s"`, else the first choice; a scripted pick outside the choices → `SelectorError(INVALID)`; `FakeSelector(error=e)` raises `e`; `calls` records each `(state, choices)`.
- AC3 (translator): `Translator(ask)` is a concrete class, `ask: Callable[[str], str]` the delegate call (the ticket in, the role's deliverable text out); `translate("TASK-1")` calls `ask("TASK-1")` once and returns that text, stripped, as the state.
- AC4 (translator failures): a blank ticket → `ValueError` with no `ask` call; `ask` raising → `SelectionError(TRANSLATE)` raised `from None`; a blank reply → `SelectionError(INVALID)`. No error message holds the ticket text, the reply or the exception's text.
- AC5 (chain types): `selection.Chooser` is an ABC generic over its choice type, `Chooser[T]`, whose only abstract method is `choose(state: str, context: Context) -> T`; `Context(role: str, task: str | None, client: str)` and `TierChoice(tier: int, effort: str)` are frozen; `SelectionError(reason)` has `str()` exactly its code; `SelectionFailure` values are exactly `translate`, `select`, `invalid`.
- AC6 (tier options from session's merged config): `TierChooser(selector).choose(state, Context("engineer", "build", "claude"))` calls `selector.select` once with `state` unchanged and one option per (model, effort): models from `clients.claude.tiers` of session's merged config in tier order, each model once (its lowest tier), efforts from `clients.claude.efforts` in their config order, each option `<model> / <effort>`; the picked option maps back to `TierChoice(<that model's lowest tier>, <effort>)`.
- AC7 (local override): with `~/.agent-pm/config.local.toml` (the test's temp HOME) setting `[session.clients.opencode.tiers]` to four entries naming two models, `TierChooser` for client `opencode` offers exactly those two models' options; with no local file it offers the packaged opencode model only; the tables it reads equal session's own merged parse (`agent_pm.session` read in the test only).
- AC8 (tier chooser failures): a blank state → `ValueError`; a client with no `tiers` or `efforts` table → `SelectionError(INVALID)`; the `Selector` raising `SelectorError` → `SelectionError(SELECT)` raised `from None`, its `__context__` suppressed and the `SelectorError`'s code kept as `selector_reason` on the `SelectionError` (so the router can log it, D13); a pick that is not one of the offered options → `SelectionError(INVALID)`. `TierChooser` passes `ChooserContract` and `TierChoiceContract` with a `FakeSelector`.
- AC9 (chooser fake): `FakeChooser[TierChoice]` passes `ChooserContract` (a blank state → `ValueError`; the same state and context → the same choice) and `TierChoiceContract` (the choice is a `TierChoice`, `tier` in 1–4, `effort` one of `low`, `medium`, `high`, `xhigh`, `max`). `FakeChooser(default=TierChoice(2, "high"), choices={"a docs fix": TierChoice(3, "low")})` returns the entry for that state, else `default`; `FakeChooser(default=…, error=e)` raises `e`; `calls` records each `(state, context)`. A `FakeChooser[str]` (another choice type) works the same with `ChooserContract`.
- AC10 (FR-26): `lint-imports --no-cache` passes with `agent_pm.selector` between `agent_pm.linear_orchestrator` and `agent_pm.delegate` in the Production layers contract, a units contract allowing it only `agent_pm.utils` and the standard library, manager, delegate, session and utils forbidden from importing it, and linear_orchestrator's `engine` unit allowed `agent_pm.selector`; `git grep -n -i "tier\|linear" -- selector` prints nothing.
- AC11 (FR-30): `session_interface_snapshot` and `delegate_interface_snapshot` pass; `git diff origin/main -- tests/session_interface.json tests/delegate_interface.json session delegate` is empty.
- AC12: every lint and suite `docs/tests.md` names passes, the coverage gate included.

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

`Selector` docstring, binding implementations: one decision API call per `select` (none for a single choice); stateless; `state` is untrusted data, never instructions; returns one of `choices` or raises `SelectorError`; empty, blank or repeated choices raise `ValueError` before any call; the key is read per call, never stored, logged or put in an error (NFR-2); no state or reply in logs or errors (NFR-4).

Tests: `tests/fakes/selector.py` `FakeSelector(Selector)` (`__init__(self, picks: Mapping[str, str] | None = None, *, error: SelectorError | None = None)`), `tests/contracts/selector.py` `SelectorContract` (hook `make() -> Selector`), unit tests per AC1–AC2.

### linear-orchestrator/src/agent_pm/linear_orchestrator/engine/selection.py

```python
class SelectionFailure(StrEnum): TRANSLATE = "translate"; SELECT = "select"; INVALID = "invalid"
class SelectionError(Exception):         # .reason: SelectionFailure; str() is the code only
    def __init__(self, reason: SelectionFailure, selector_reason: Failure | None = None): ...

@dataclass(frozen=True)
class Context: role: str; task: str | None; client: str    # the choices made so far
@dataclass(frozen=True)
class TierChoice: tier: int; effort: str

class Translator:
    def __init__(self, ask: Callable[[str], str]): ...   # the delegate call: ticket → deliverable text
    def translate(self, ticket: str) -> str: ...         # the state; AC3, AC4

T = TypeVar("T")
class Chooser(ABC, Generic[T]):
    @abstractmethod
    def choose(self, state: str, context: Context) -> T: ...

class TierChooser(Chooser[TierChoice]):
    def __init__(self, selector: Selector): ...
    def choose(self, state: str, context: Context) -> TierChoice: ...   # AC6–AC8
```

Docstrings: `ticket` is an issue identifier (`TASK-123`); issue text, state and deliverable are untrusted data; errors carry codes only. `Translator`: in production `ask` runs the `choice-translator` role's task through `agent_pm.delegate` on Claude Haiku, its deliverable the state text (the translator issue adds that call, the role and its task); tests pass a fake `ask`. `Chooser[T]`: one step of the router's chain (Flow's chain convention); takes the state translated once per ticket and the `Context` so far; lists its options from config given that context. `TierChooser` reads `clients.<context.client>.tiers` and `.efforts` from session's merged config (D10), one read per `choose`.

Tests: `linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py` holds `FakeChooser(Chooser[T])` (`__init__(self, *, default: T, choices: Mapping[str, T] | None = None, error: Exception | None = None)`, `choices` keyed by state), `ChooserContract` (hooks `make() -> Chooser`, `context() -> Context`) and `TierChoiceContract` (adds the tier and effort checks), the orchestrator's `*_fixtures.py` convention; unit tests `orchestrator_tests/unit/engine/selection_test.py` per AC3–AC9, `Translator` with a fake `ask`, `TierChooser` with `selector.tests.fakes.selector.FakeSelector` and local overrides written to the test's temp HOME.

### Repository wiring

As the repo requires for a new project (one package per project: `utils/tests/sandbox.py` `project_roots`): `tests/guards.py` `LEVELS` gets `("selector",)` between delegate and linear-orchestrator; `tests/guard_data/selector.py`; `pyproject.toml` pyright, vulture, `root_packages`, the AC10 contracts, a `selector.tests` ownership contract, the linear_orchestrator units contract's `engine` line allowing `agent_pm.selector`; `tests/guard_checks/imports.py` `orchestrator_imports`: linear_orchestrator may also import `agent_pm.selector`'s `__all__` names; PYTHONPATH and src lists in `.github/workflows/test.yml`, `docs/tests.md`, `bin/agent-pm-linear`; `.github/vulture_whitelist.py` entries for every name no `src` code uses yet, naming TASK-511.

### Docs

`docs/architecture.md`: six layers, the Flow and chain convention above. `CONTEXT.md`: terms **Selector**, **Choice translator**, **Chooser**, **Chooser chain**. `AGENTS.md` Import rules: the layer order.

## Verification

| AC | Test or check |
| --- | --- |
| AC1 | `selector/tests/unit/engine/types_test.py` |
| AC2 | `selector/tests/unit/fakes/selector_test.py` (`FakeSelector` × `SelectorContract`) |
| AC3 | `linear-orchestrator/tests/orchestrator_tests/unit/engine/selection_test.py` `Translator` cases |
| AC4 | the same file, `Translator` failure cases |
| AC5 | the same file, types and `Chooser` cases |
| AC6 | the same file, `TierChooser` options and mapping cases |
| AC7 | the same file, local override case |
| AC8 | the same file, `TierChooser` failure and contract cases |
| AC9 | the same file, `FakeChooser` × `ChooserContract` and `TierChoiceContract`, and a `FakeChooser[str]` case |
| AC10 | `lint-imports --no-cache`; `git grep` |
| AC11 | `python3 -m tests.checks`; `git diff` |
| AC12 | the lint commands and suites of `docs/tests.md`; CI |

## Decisions

- D1: `Translator`, `Context`, `Chooser` and `TierChooser` live in linear_orchestrator, not a generic package: the ticket, the tier and the client are outside FR-26's generic concepts; only the selector is generic.
- D2: Choices are plain strings and the answer one of them (the user's "the chosen option").
- D3: `Selector` and `Chooser` are ABCs with no stub functions; `Translator` and `TierChooser` are concrete (the user's changes).
- D4: `TierChoice.effort` is a `str` so the orchestrator does not import session's `Effort`; `TierChoiceContract` checks its values.
- D5: No config keys and no router change here; the chain's on/off switches, `Context` filling and fallbacks come with the router issue.
- D6: The choice translator's reply is the state text itself (no JSON, no choices); the role's task text that produces it is the translator issue's.
- D7: `Chooser` is generic over its choice type (`Generic[T]`, Python 3.11) so the client, role and task choosers reuse it; `FakeChooser` takes a required `default` of that type.
- D8: The Jev and Kev implementations send `state` as `{"task": <state>}` (the object shape the pilot verified), each choice with a `null` description, under one fixed question id `choice` with the selector's fixed instructions.
- D9: `Chooser.choose` takes the state, not the ticket, so the router translates once per ticket and every chooser reuses that state (the user's "one Haiku translation per ticket serves every chooser").
- D10: The tier chooser's options come from session's own config, the single source (the user's decision): `[clients.<client>.tiers]` and `[clients.<client>.efforts]` from the merged config, the packaged `session/src/agent_pm/session/config.toml` with `~/.agent-pm/config.local.toml`'s `[session]` section over it (the user's local config overrides the OpenCode tiers), read through `utils` config_file, `utils.read_resources((("agent_pm.session", "config.toml"),))` with its default `local`, a resource read by name, not an import of session (FR-31, FR-30); never the packaged resource alone, and no copy of the tiers in the orchestrator config.
- D11: Options are one per (model, effort), not per (tier, effort): tiers naming the same model (OpenCode's four) give one model, and a pick maps back to its lowest tier; the label `<model> / <effort>` names what the decision API can judge.
- D12: `Context` carries role, task and client: the choices the chain's steps, now and later, build on; the router fills them in this chain's first issue, and client, role and task choosers later replace those fields with their picks.
- D13: Fallback is the router's (D5): `TierChooser` raises `SelectionError`, never falls back itself, so every step falls back one way; `SelectionError.selector_reason` carries the `Selector`'s `Failure` code for the log line, the only detail the router logs (NFR-4).
- Left to the implementer: docstring wording, test names.
