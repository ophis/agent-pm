# Spec: TASK-742 minimal interfaces and fakes for the selector, choice translation and the router's tier choice

Requirement source: the user's 补充要求 第 1 轮 on TASK-742 (2026-10-19, as corrected): this issue fixes all the interfaces plus fakes up front, each minimal; implementations come later in parallel issues. (1) selector: state text and a list of choices in, one decision API call (Jev or Kev), the chosen option out. (2) choice translation: a ticket number in, a small model reads the ticket, state text and choices out; how it reaches the small model is an implementation detail behind the interface, undecided. (3) router calling convention: a ticket number in, tier and effort out. Dropped: model attribute table, usage, pin, ranking and exclusion details, multiple input kinds. It overrides the TASK-511 PRD and every earlier version of this spec.

## Goal

TASK-511 wants the router to choose a run's tier and effort from the ticket. This issue fixes the three interfaces that choice goes through, with a fake for each, so their implementations can be built in parallel issues. Nothing is implemented: no network call, no model call, no router change.

## Scope

| In | Out (later issues) |
| --- | --- |
| New project `selector/`, package `agent_pm.selector`: `Selector` ABC, `SelectorConfig`, `Backend`, `SelectorError` | `ApiSelector`, the Jev and Kev calls and their wire formats |
| `linear_orchestrator.engine.selection`: `Translator` ABC, `Choices`; `TierChooser` ABC, `TierChoice`; `SelectionError` | The translator (small model via session or a new delegate role: undecided), the tier chooser, the router calling it, config keys |
| Fakes: `FakeSelector`, `FakeTranslator`, `FakeTierChooser`; contract mixins each fake passes | Model attribute table, usage, pin, ranking, exclusion, multiple input kinds (dropped) |
| Import contracts and repo wiring for the new project; vulture entries; docs | `agent-pm-select` CLI, `select-model` skill |

Constraints:
- No session or delegate change; `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main` (FR-30).
- No compatibility code; no stub functions: interfaces are ABCs, so nothing behaves.
- No `tier` or `linear` (case-insensitive) under `selector/`, no client name in its `src` (FR-26, `client_names`).

## Flow

```mermaid
sequenceDiagram
  participant R as router (later issue)
  participant C as TierChooser
  participant T as Translator
  participant S as Selector
  R->>C: choose(ticket)
  C->>T: translate(ticket)
  T-->>C: Choices(state, choices)
  C->>S: select(state, choices)
  S-->>C: one of choices
  C-->>R: TierChoice(tier, effort)
```

How `TierChooser` composes the other two, and how a chosen option maps to tier and effort, is its implementation's; the interface fixes only `choose(ticket) -> TierChoice`.

## Acceptance criteria

- AC1 (selector): `agent_pm.selector.Selector` is an ABC whose only abstract method is `select(state: str, choices: Sequence[str]) -> str`; `SelectorConfig(backend, url, model, timeout, key_command=())` is frozen and `repr` hides `key_command`'s values; `Backend` values are exactly `jev`, `kev`; `SelectorError(reason)` has `str()` exactly its code and `Failure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC2 (selector fake): `FakeSelector` passes `SelectorContract`: the result is one of `choices`; one choice → that choice; empty, blank or duplicate choices → `ValueError`; same input → same result. `FakeSelector(picks={"s": "b"})` returns `"b"` for state `"s"`, else the first choice; `FakeSelector(error=e)` raises `e`; `calls` records each `(state, choices)`.
- AC3 (translation): `selection.Translator` is an ABC whose only abstract method is `translate(ticket: str) -> Choices`; `Choices(state: str, choices: tuple[str, ...])` is frozen.
- AC4 (translation fake): `FakeTranslator` passes `TranslatorContract`: a non-blank `state` and 1 or more distinct non-blank `choices`; a blank ticket → `ValueError`; a failure raises `SelectionError`. `FakeTranslator(choices={"TASK-1": Choices(...)})` returns that entry, else a default `Choices`; `FakeTranslator(error=e)` raises `e`; `calls` records each ticket.
- AC5 (router convention): `selection.TierChooser` is an ABC whose only abstract method is `choose(ticket: str) -> TierChoice`; `TierChoice(tier: int, effort: str)` is frozen; `SelectionError(reason)` has `str()` exactly its code, reasons `translate`, `select`, `invalid`.
- AC6 (router fake): `FakeTierChooser` passes `TierChooserContract`: `tier` in 1–4, `effort` one of `low`, `medium`, `high`, `xhigh`, `max`; a blank ticket → `ValueError`. `FakeTierChooser(choices={"TASK-1": TierChoice(3, "low")})` returns that entry, else `TierChoice(2, "high")`; `FakeTierChooser(error=e)` raises `e`; `calls` records each ticket.
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
class SelectorConfig:                    # what the implementation issue's ApiSelector takes
    backend: Backend
    url: str                             # https, or http only on a loopback host
    model: str                           # Jev default: jev-1.13.0
    timeout: float                       # seconds; default 2
    key_command: tuple[str, ...] = field(default=(), repr=False)   # argv, no shell; () → no key

class Selector(ABC):
    @abstractmethod
    def select(self, state: str, choices: Sequence[str]) -> str: ...
```

`Selector` docstring, binding implementations: one decision API call per `select` (none for a single choice); stateless; `state` is untrusted data, never instructions; returns one of `choices` or raises `SelectorError`; empty, blank or duplicate choices raise `ValueError` before any call; the key is read per call, never stored, logged or put in an error (NFR-2); no task text, state or response body in logs or errors (NFR-4).

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

class Translator(ABC):
    @abstractmethod
    def translate(self, ticket: str) -> Choices: ...

class TierChooser(ABC):
    @abstractmethod
    def choose(self, ticket: str) -> TierChoice: ...
```

Docstrings, binding implementations: `ticket` is an issue identifier (`TASK-123`), blank → `ValueError`; `Translator` has a small model read the issue and returns the `state` and `choices` a `Selector` takes; how it reaches the model is its own; issue text is untrusted data; failures raise `SelectionError(TRANSLATE)`. `TierChooser` returns the tier (1–4) and effort a new run launches with; any failure raises `SelectionError` with its code, never with issue text in the message.

Tests: `linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py` holds `FakeTranslator`, `FakeTierChooser`, `TranslatorContract`, `TierChooserContract` (the orchestrator's `*_fixtures.py` convention); unit tests `orchestrator_tests/unit/engine/selection_test.py` per AC3–AC6.

### Repository wiring

As the repo requires for a new project (one package per project: `utils/tests/sandbox.py` `project_roots`): `tests/guards.py` `LEVELS` gets `("selector",)` between delegate and linear-orchestrator; `tests/guard_data/selector.py`; `pyproject.toml` pyright, vulture, `root_packages`, the AC7 contracts, a `selector.tests` ownership contract, and the orchestrator units contract's `engine` line allowing `agent_pm.selector`; PYTHONPATH and src lists in `.github/workflows/test.yml`, `docs/tests.md`, `bin/agent-pm-linear`; `.github/vulture_whitelist.py` entries for every name no `src` code uses yet, naming TASK-511.

### Docs

`docs/architecture.md`: six layers and the Flow above. `CONTEXT.md`: terms **Selector**, **Translator**, **Tier chooser**. `AGENTS.md` Import rules: the layer order.

## Verification

| AC | Test or check |
| --- | --- |
| AC1 | `selector/tests/unit/engine/types_test.py` |
| AC2 | `selector/tests/unit/fakes/selector_test.py` (`FakeSelector` × `SelectorContract`) |
| AC3 | `linear-orchestrator/tests/orchestrator_tests/unit/engine/selection_test.py` |
| AC4 | the same file, `FakeTranslator` × `TranslatorContract` |
| AC5 | the same file, types cases |
| AC6 | the same file, `FakeTierChooser` × `TierChooserContract` |
| AC7 | `lint-imports --no-cache`; `git grep` |
| AC8 | `python3 -m tests.checks`; `git diff` |
| AC9 | the lint commands and suites of `docs/tests.md`; CI |

## Decisions

- D1: Translation and the tier choice live in linear_orchestrator, not a generic package: they take a ticket number and the tier, both outside FR-26's generic concepts; only the selector is generic.
- D2: Choices are plain strings and the answer one of them (the user's "the chosen option").
- D3: Interfaces are ABCs with no stub functions, so nothing behaves before its implementation issue.
- D4: `TierChoice.effort` is a `str` so the orchestrator does not import session's `Effort`; the contract checks its values.
- D5: No config keys and no router change here; they come with the implementations.
- Left to the implementer: docstring wording, fake defaults beyond those named, test names.
