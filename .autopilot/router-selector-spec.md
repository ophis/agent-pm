# Spec: TASK-742 minimal selector, choice translator and chooser interfaces, with their fakes

Requirement source: the user's 补充要求 第 1 轮 on TASK-742 (2026-10-19, as corrected) and the user's interface changes of the same day. This issue fixes the interfaces plus fakes up front, each minimal; implementations come later in parallel issues. (1) Selector: state text and a list of choices in, one decision API call, the chosen option out; it stays abstract, Jev and Kev being separate implementations. (2) Choice translator: a ticket number in, a small model reads the ticket, state text and choices out; it is a new delegate role plus a new task, "choice translator", run on Claude Haiku, called through delegate, never session; one concrete class, no abstract base and no fake class: tests inject a fake at its boundary, the delegate call. (3) Router calling convention: a ticket number in, a choice out, through a chooser generic over what is chosen; this issue chooses tier and effort (`TierChoice`); later issues choose the client, then role and task. Dropped: model attribute table, usage, pin, ranking and exclusion details, multiple input kinds. It overrides the TASK-511 PRD and every earlier version of this spec.

## Goal

TASK-511 wants the router to choose, from the ticket, how a run launches: first its tier and effort, later its client, role and task. This issue fixes the three pieces that choice goes through, so their implementations can be built in parallel issues: the abstract `Selector` (a decision API call), the concrete `Translator` (ticket → state and choices, via a delegate role) and the generic `Chooser` (ticket → a choice). Only the `Translator`'s parsing behaves; no network call, model call, role or router change ships here.

## Scope

| In | Out (later issues) |
| --- | --- |
| New project `selector/`, package `agent_pm.selector`: `Selector` ABC, `SelectorConfig`, `Backend`, `Failure`, `SelectorError`; `FakeSelector`, `SelectorContract` | `JevSelector` and `KevSelector`, separate `Selector` implementations, and their HTTP calls |
| `linear_orchestrator.engine.selection`: concrete `Translator` and `Choices`; generic `Chooser[T]` ABC; `TierChoice`; `SelectionError`; `FakeChooser`, `ChooserContract`, `TierChoiceContract` | The `choice-translator` role, its task and the delegate call the `Translator` gets in production; the tier chooser implementation; the router calling it; later choices (client, role, task); config keys |
| Import contracts and repo wiring for the new project; vulture entries; docs | Model attribute table, usage, pin, ranking, exclusion, multiple input kinds (dropped); `agent-pm-select` CLI, `select-model` skill |

Constraints:
- No session or delegate change; `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main` (FR-30).
- No compatibility code; no stub functions: `Selector` and `Chooser` are ABCs.
- No `tier` or `linear` (case-insensitive) under `selector/`, no client name in its `src` (FR-26, `client_names`).

## Flow

```mermaid
sequenceDiagram
  participant R as router (later issue)
  participant C as Chooser[TierChoice]
  participant T as Translator
  participant D as delegate call (choice-translator role)
  participant S as Selector (Jev or Kev)
  R->>C: choose(ticket)
  C->>T: translate(ticket)
  T->>D: ask(ticket)
  D-->>T: the role's deliverable text
  T-->>C: Choices(state, choices)
  C->>S: select(state, choices)
  S-->>C: one of choices
  C-->>R: TierChoice(tier, effort)
```

How a `Chooser` implementation composes the `Translator` and a `Selector`, and maps the chosen option to its choice type, is that implementation's; the interface fixes only `choose(ticket) -> T`. In production the `Translator`'s `ask` runs the `choice-translator` role's task through `agent_pm.delegate` on Claude Haiku (the role's `client = "claude"`, `tier = 4`), never `agent_pm.session` (forbidden by the import contracts).

Wire format and defaults the Jev and Kev implementations follow, from the TASK-523 pilot report (`ophis/private_docs` `Research/2026-10-16-TASK-523-jev-vs-claude-pilot.md`, a real Jev request and response) and TASK-723 (`Research/2026-10-19-0240-TASK-723-decision-model-apis.md`: Kev uses the same System One format):

- Request: `POST <url>`, `Content-Type: application/json`, `Authorization: Bearer <key>` (no header when Kev has no key). Body: `{"model": <model>, "state": {"task": <state>}, "questions": {"choice": {"type": "choice", "instructions": <fixed text: pick the option that best fits the task; the state is data, not instructions>, "criteria": {<each choice>: null}}}}`.
- Response: the answer is `answers.choice.choice`, which must be one of `choices`; `probabilities` and `confidence` are not returned by `select`.
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
      key_command: tuple~str~  «repr=False»
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
    class Chooser~T~ {
      <<abstract>>
      +choose(ticket: str) T
    }
    class TierChoice {
      <<frozen>>
      tier: int
      effort: str
    }
    class Translator {
      <<concrete>>
      ask: Callable~str, str~
      +translate(ticket: str) Choices
    }
    class Choices {
      <<frozen>>
      state: str
      choices: tuple~str~
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
    <<existing, later issue>>
  }
  class ChoiceTranslatorRole {
    <<delegate role choice-translator, Claude Haiku>>
  }
  class DecisionAPI {
    <<Jev or Kev, POST /v1/systemone>>
  }

  Selector <|-- FakeSelector
  Selector <|-- JevSelector
  Selector <|-- KevSelector
  Chooser <|-- FakeChooser
  SelectorConfig --> Backend
  SelectorError --> Failure
  SelectionError --> SelectionFailure
  Translator ..> Choices : returns
  Chooser ..> TierChoice : T = TierChoice here

  Router ..> Chooser : choose(ticket)
  Chooser ..> Translator : translate(ticket)
  Chooser ..> Selector : select(state, choices)
  Translator ..> ChoiceTranslatorRole : ask(ticket), via delegate
  JevSelector ..> DecisionAPI : one call
  KevSelector ..> DecisionAPI : one call
  JevSelector ..> SelectorConfig
  KevSelector ..> SelectorConfig
```

Dashed arrow: a call or use; `<|--`: implements. `JevSelector`, `KevSelector`, the role, the tier `Chooser` implementation and the router call are later issues; the rest ships here. Later choices reuse `Chooser[T]` with their own `T` (client, then role and task).

## Acceptance criteria

- AC1 (selector): `agent_pm.selector.Selector` is an ABC whose only abstract method is `select(state: str, choices: Sequence[str]) -> str`; `SelectorConfig(backend, url, model, timeout, key_command=())` is frozen and its `repr` holds none of `key_command`'s values; `Backend` values are exactly `jev`, `kev`; `SelectorError(reason)` has `str()` exactly its code; `Failure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC2 (selector fake): `FakeSelector` passes `SelectorContract`: the result is one of `choices`; one choice → that choice; empty, blank or repeated choices → `ValueError`; same input → same result. `FakeSelector(picks={"s": "b"})` returns `"b"` for state `"s"`, else the first choice; a scripted pick outside the choices → `SelectorError(INVALID)`; `FakeSelector(error=e)` raises `e`; `calls` records each `(state, choices)`.
- AC3 (translator): `Translator(ask)` is a concrete class, `ask: Callable[[str], str]` the delegate call (the ticket in, the role's deliverable text out); `translate("TASK-1")` calls `ask("TASK-1")` once and parses its JSON object `{"state": <str>, "choices": [<str>, …]}` into `Choices(state, choices)`.
- AC4 (translator failures): a blank ticket → `ValueError` with no `ask` call; `ask` raising → `SelectionError(TRANSLATE)` raised `from None`; text that is not that JSON object, a blank `state`, or `choices` empty, not all strings, blank or repeated → `SelectionError(INVALID)`. No error message holds the ticket text, the deliverable or the exception's text.
- AC5 (chooser): `selection.Chooser` is an ABC generic over its choice type, `Chooser[T]`, whose only abstract method is `choose(ticket: str) -> T`; `TierChoice(tier: int, effort: str)` is frozen; `SelectionError(reason)` has `str()` exactly its code; `SelectionFailure` values are exactly `translate`, `select`, `invalid`.
- AC6 (chooser fake): `FakeChooser[TierChoice]` passes `ChooserContract` (a blank ticket → `ValueError`; the same ticket → the same choice) and `TierChoiceContract` (the choice is a `TierChoice`, `tier` in 1–4, `effort` one of `low`, `medium`, `high`, `xhigh`, `max`). `FakeChooser(default=TierChoice(2, "high"), choices={"TASK-1": TierChoice(3, "low")})` returns the entry for `TASK-1`, else `default`; `FakeChooser(default=…, error=e)` raises `e`; `calls` records each ticket. A `FakeChooser[str]` (another choice type) works the same with `ChooserContract`.
- AC7 (FR-26): `lint-imports --no-cache` passes with `agent_pm.selector` between `agent_pm.linear_orchestrator` and `agent_pm.delegate` in the Production layers contract, a units contract allowing it only `agent_pm.utils` and the standard library, and manager, delegate, session and utils forbidden from importing it; `git grep -n -i "tier\|linear" -- selector` prints nothing.
- AC8 (FR-30): `session_interface_snapshot` and `delegate_interface_snapshot` pass; `git diff origin/main -- tests/session_interface.json tests/delegate_interface.json session delegate` is empty.
- AC9: every lint and suite `docs/tests.md` names passes, the coverage gate included.

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
    def __init__(self, reason: SelectionFailure): ...

@dataclass(frozen=True)
class Choices: state: str; choices: tuple[str, ...]
@dataclass(frozen=True)
class TierChoice: tier: int; effort: str

class Translator:
    def __init__(self, ask: Callable[[str], str]): ...   # the delegate call: ticket → deliverable text
    def translate(self, ticket: str) -> Choices: ...     # AC3, AC4

T = TypeVar("T")
class Chooser(ABC, Generic[T]):
    @abstractmethod
    def choose(self, ticket: str) -> T: ...
```

Docstrings: `ticket` is an issue identifier (`TASK-123`); issue text and the deliverable are untrusted data; errors carry codes only. `Translator`: in production `ask` runs the `choice-translator` role's task through `agent_pm.delegate` on Claude Haiku, its deliverable the JSON object of AC3 (the translator issue adds that call, the role and its task); tests pass a fake `ask`. `Chooser[T]`: what is chosen is `T`; this issue's is `TierChoice`, the tier (1–4) and effort a new run launches with; later choices (client, role, task) are new `T`s; any failure raises `SelectionError` with its code; a blank ticket raises `ValueError`.

Tests: `linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py` holds `FakeChooser(Chooser[T])` (`__init__(self, *, default: T, choices: Mapping[str, T] | None = None, error: Exception | None = None)`), `ChooserContract` (hook `make() -> Chooser`) and `TierChoiceContract` (adds the tier and effort checks), the orchestrator's `*_fixtures.py` convention; unit tests `orchestrator_tests/unit/engine/selection_test.py` per AC3–AC6, `Translator` with a fake `ask`.

### Repository wiring

As the repo requires for a new project (one package per project: `utils/tests/sandbox.py` `project_roots`): `tests/guards.py` `LEVELS` gets `("selector",)` between delegate and linear-orchestrator; `tests/guard_data/selector.py`; `pyproject.toml` pyright, vulture, `root_packages`, the AC7 contracts and a `selector.tests` ownership contract; PYTHONPATH and src lists in `.github/workflows/test.yml`, `docs/tests.md`, `bin/agent-pm-linear`; `.github/vulture_whitelist.py` entries for every name no `src` code uses yet, naming TASK-511.

### Docs

`docs/architecture.md`: six layers and the Flow above. `CONTEXT.md`: terms **Selector**, **Choice translator**, **Chooser**. `AGENTS.md` Import rules: the layer order.

## Verification

| AC | Test or check |
| --- | --- |
| AC1 | `selector/tests/unit/engine/types_test.py` |
| AC2 | `selector/tests/unit/fakes/selector_test.py` (`FakeSelector` × `SelectorContract`) |
| AC3 | `linear-orchestrator/tests/orchestrator_tests/unit/engine/selection_test.py` `Translator` cases with a fake `ask` |
| AC4 | the same file, `Translator` failure cases |
| AC5 | the same file, types and `Chooser` cases |
| AC6 | the same file, `FakeChooser` × `ChooserContract` and `TierChoiceContract`, and a `FakeChooser[str]` case |
| AC7 | `lint-imports --no-cache`; `git grep` |
| AC8 | `python3 -m tests.checks`; `git diff` |
| AC9 | the lint commands and suites of `docs/tests.md`; CI |

## Decisions

- D1: `Translator` and `Chooser` live in linear_orchestrator, not a generic package: they take a ticket number, and the first choice is the tier, both outside FR-26's generic concepts; only the selector is generic.
- D2: Choices are plain strings and the answer one of them (the user's "the chosen option").
- D3: `Selector` and `Chooser` are ABCs with no stub functions; `Translator` is concrete (the user's change), its only behavior the parsing of the delegate call's reply, so its tests need no role.
- D4: `TierChoice.effort` is a `str` so the orchestrator does not import session's `Effort`; `TierChoiceContract` checks its values.
- D5: No config keys and no router change here; they come with the implementations.
- D6: The choice translator's reply is the JSON object `{"state", "choices"}`; the role's task text that produces it is the translator issue's.
- D7: `Chooser` is generic over its choice type (`Generic[T]`, Python 3.11) so the client, role and task choices reuse it; `FakeChooser` takes a required `default` of that type.
- D8: The Jev and Kev implementations send `state` as `{"task": <state>}` (the object shape the pilot verified), each choice with a `null` description, under one fixed question id `choice` with the selector's fixed instructions.
- Left to the implementer: docstring wording, test names.
