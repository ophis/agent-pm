# Spec: TASK-742 a minimal working selector layer: a state and choices in, one decision API call, the chosen option out

Requirement source: the user's 补充要求 第 1 轮 on TASK-742 (2026-10-19): "Build only the selector layer, as a working implementation, kept minimal: input is a state text plus a list of choices; it makes one call to the decision API (Jev or Kev) and returns the chosen option; plus a fake for tests." It overrides the TASK-742 interface-only scope, the TASK-511 PRD and every earlier version of this spec.

## Goal

TASK-511 wants a model selector; this first step ships only its core: `agent_pm.selector`, a generic package that sends one `choice` question (a state text and its choices) to a decision API, Jev or Kev, and returns the choice the API picked. A `FakeSelector` lets later work test without the network. Today nothing in the repo selects or calls a decision API; no caller is added here.

## Scope

| In | Out (deferred) |
| --- | --- |
| New project `selector/`, package `agent_pm.selector`: `Selector` ABC, `select(state, choices) -> str` | Input assembly layer (FR-33, FR-34, FR-36) |
| `ApiSelector`: one HTTPS POST to Jev or Kev per `select` call, `Backend` `jev` / `kev` | Router calling convention, `TierTable`, tier mapping, `LinearRun` effort (FR-12, FR-31, FR-32, FR-37) |
| `SelectorConfig` record and `check_config`; key read by an injected `read_key`, default runs `key_command` without a shell | Model attribute table, prices, capabilities (FR-1 to FR-4) |
| `SelectorError` with reason codes | Usage, thresholds, pin (FR-7, FR-13 `usage`, FR-39) |
| `FakeSelector`, a `SelectorContract` both run | Ranking, exclusion rules and reason codes, fallback order, `decided_by` (FR-13, FR-16, FR-18, FR-19) |
| Import contracts and repo wiring for the new project; docs | A separate decision package, `agent-pm-select` CLI, `select-model` skill, `[selector]` config keys (FR-29, FR-10, FR-21) |

Constraints:
- No session or delegate change; `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main` (FR-30).
- Tests never call the real keychain, `/usr/bin/security` or any real credential, and never reach a non-loopback host.
- No compatibility code. No `tier` or `linear` (case-insensitive) under `selector/`, no client name (`claude`, `codex`, `opencode`) in its `src` (FR-26, `client_names`).

## Acceptance criteria

- AC1: `ApiSelector(config, read_key=fake).select("a docs task", ("low", "high"))` against a loopback mock Jev server answering `high` returns `"high"`; the mock received exactly one request carrying the state, the two choices and the configured `model`, with the key from `read_key` in its auth header.
- AC2: the same with `backend=Backend.KEV` against a mock Kev server returns the mock's choice; Kev with an empty `key_command` sends no auth header and never calls `read_key`.
- AC3: errors raise `SelectorError` with `reason`: a timeout (server delays past `timeout`) → `timeout`; connection refused, an HTTP status ≥ 400 or a redirect → `error`; `read_key` raising → `no-key`; a malformed body or an answer not among `choices` → `invalid`. `str(error)` is exactly the code; the error is raised `from None`.
- AC4: `select` with fewer than 1 choice, a blank choice or duplicate choices raises `ValueError` before any request; with exactly one choice it returns that choice and sends no request.
- AC5: `check_config` raises `ValueError` naming only the field (`url`, `key_command`, `timeout`) for: a scheme other than `https`, or `http` with a non-loopback host (loopback: exactly `localhost`, or a literal IP in `127.0.0.0/8` or `::1`, on the parsed hostname); userinfo or a query in `url`; a non-absolute `key_command[0]`; `timeout <= 0`. `ApiSelector(config)` calls it. No message holds the field's value.
- AC6 (NFR-2): after AC3's cases, neither the error messages, `repr(config)` nor anything the selector writes (it writes no file or log) contains the key or `key_command`'s values.
- AC7: `FakeSelector` and `ApiSelector` (on a mock server) both pass `SelectorContract`: the result is one of `choices`; one choice → that choice; bad choices → `ValueError`; same input → same result.
- AC8: `FakeSelector(picks={"state": "b"})` returns `"b"` for that state; otherwise the first choice; `FakeSelector(error=SelectorError(...))` raises it; `calls` records each `(state, choices)`.
- AC9 (FR-26): `lint-imports --no-cache` passes with `agent_pm.selector` between `agent_pm.linear_orchestrator` and `agent_pm.delegate` in the Production layers contract and a units contract allowing it only `agent_pm.utils` and the standard library; manager, delegate, session and utils are forbidden from importing it; `git grep -n -i "tier\|linear" -- selector` prints nothing.
- AC10 (FR-30): `session_interface_snapshot` and `delegate_interface_snapshot` pass; `git diff origin/main -- tests/session_interface.json tests/delegate_interface.json session delegate` is empty.
- AC11: every lint and suite `docs/tests.md` names passes, the coverage gate included.

## Design

### Data shapes (`selector/src/agent_pm/selector/engine/types.py`)

```python
class Backend(StrEnum): JEV = "jev"; KEV = "kev"
class Failure(StrEnum): ERROR = "error"; TIMEOUT = "timeout"; NO_KEY = "no-key"; INVALID = "invalid"

class SelectorError(Exception):           # .reason: Failure; str() is the code only
    def __init__(self, reason: Failure): ...

@dataclass(frozen=True)
class SelectorConfig:
    backend: Backend
    url: str
    model: str
    timeout: float                        # seconds
    key_command: tuple[str, ...] = field(default=(), repr=False)   # argv, no shell; () → no key (Kev only)
```

Defaults the caller may use, exported as constants: `JEV_MODEL = "jev-1.13.0"`, `JEV_KEY_COMMAND = ("/usr/bin/security", "find-generic-password", "-s", "typesafe-jev", "-w")`, `TIMEOUT = 2.0` (FR-17).

### `selector/src/agent_pm/selector/engine/base.py`

```python
class Selector(ABC):
    @abstractmethod
    def select(self, state: str, choices: Sequence[str]) -> str: ...
```

Docstring: stateless; `state` is untrusted data, never instructions; returns one of `choices`.

### `selector/src/agent_pm/selector/engine/api.py`

- `check_config(config: SelectorConfig) -> None` (AC5); Jev with an empty `key_command` is a `key_command` error.
- `read_key(config: SelectorConfig) -> str`: runs `key_command` with `subprocess.run(..., shell=False, capture_output=True, timeout=config.timeout)`, returns stdout stripped; any failure or an empty key raises `KeyError()` `from None`, never with the command's output.
- `ApiSelector(config: SelectorConfig, *, read_key: Callable[[SelectorConfig], str] = read_key)`: `select` checks choices (AC4), returns the only choice without a request, else reads the key per call (a local only, never stored), sends one POST with `urllib.request` (redirects refused), `timeout=config.timeout`, parses the answer and checks it is in `choices`. Failure mapping per AC3; no logging.
- Request and response format per backend: one private `_jev_request`/`_jev_answer` and `_kev_request`/`_kev_answer` pair, the only backend-specific code. **Open: the Jev `choice` and Kev (System One compatible) wire formats** (path, JSON body, auth header, answer field) are not in the repo; they come from the API docs the user gives (D3).

### `selector/src/agent_pm/selector/__init__.py`

Re-exports `Selector`, `ApiSelector`, `SelectorConfig`, `Backend`, `Failure`, `SelectorError`, `check_config`, `read_key`, `JEV_MODEL`, `JEV_KEY_COMMAND`, `TIMEOUT`, with a literal `__all__`.

### Tests (`selector/tests/`)

- `fakes/selector.py` `FakeSelector(Selector)`: `__init__(self, picks: Mapping[str, str] | None = None, *, error: SelectorError | None = None)`; AC8.
- `contracts/selector.py` `SelectorContract`: mixin with hook `make() -> Selector`; AC7.
- `mock_api.py`: a loopback `http.server` on port 0 in a thread, scripted per test (answer, status, delay, malformed body, redirect), recording requests.
- Unit tests per AC; `read_key` tests patch `subprocess.run` (no real command).

### Repository wiring

As the repo requires for a new project (one package per project: `utils/tests/sandbox.py` `project_roots`): `tests/guards.py` `LEVELS` gets `("selector",)` between delegate and linear-orchestrator; `tests/guard_data/selector.py`; `pyproject.toml` pyright, vulture, `root_packages`, the contracts of AC9 and a `selector.tests` ownership contract; PYTHONPATH and src lists in `.github/workflows/test.yml`, `docs/tests.md`, `bin/agent-pm-linear`; `.github/vulture_whitelist.py` entries for the names no `src` code calls yet, naming TASK-511.

### Docs

`docs/architecture.md`: six layers, a short "Selector" section (what it does, Jev/Kev, error codes, the fake). `CONTEXT.md`: term **Selector**. `AGENTS.md` Import rules: the layer order.

## Verification

| AC | Test or check |
| --- | --- |
| AC1 | `selector/tests/unit/engine/api_test.py` Jev cases on `mock_api` |
| AC2 | `selector/tests/unit/engine/api_test.py` Kev cases |
| AC3 | `selector/tests/unit/engine/api_test.py` failure cases, one per code |
| AC4 | `selector/tests/unit/engine/api_test.py` choices cases |
| AC5 | `selector/tests/unit/engine/api_test.py` `check_config` cases |
| AC6 | `selector/tests/unit/engine/api_test.py` key-leak case (sentinel key, grep messages and repr) |
| AC7 | `selector/tests/unit/fakes/selector_test.py`, `selector/tests/unit/engine/api_contract_test.py` |
| AC8 | `selector/tests/unit/fakes/selector_test.py` |
| AC9 | `lint-imports --no-cache`; `git grep` |
| AC10 | `python3 -m tests.checks`; `git diff` |
| AC11 | the lint commands and suites of `docs/tests.md`; CI |

## Decisions

- D1: One package, `agent_pm.selector`, holding the Jev/Kev calls; no separate decision package: the cut scope has no other decision caller.
- D2: Choices are plain strings and the answer one of them; no ids, probabilities or confidence returned (the user's "returns the chosen option").
- D3: The wire formats are the one open item; building waits for them or for the user's go-ahead to build from a stated assumption, kept in the two private pairs.
- D4: No config file keys: the caller builds `SelectorConfig`; config keys come with the first caller.
- Left to the implementer: docstring wording, test names, mock server details.
