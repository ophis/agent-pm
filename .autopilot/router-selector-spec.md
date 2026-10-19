# Spec: TASK-742 interfaces of the selector, assemble and decision layers and the router's selector call

Requirement source: the plan doc's `## Input` (TASK-742, the user's instructions and the TASK-511 PRD rows FR-26, FR-27, FR-29, FR-30, FR-39 quoted there). It overrides any other input.

## Goal

TASK-511 adds a model selector that the router calls before launching a run. Five follow-up issues (TASK-743 selector core, TASK-745 assemble layer, TASK-747 router integration, the decision backends, the CLI and skill) must start in parallel. This change gives them a fixed, checkable interface: three new generic projects holding only types and abstract interfaces, the orchestrator-side records of the router's call, import contracts, a frozen interface snapshot, and shared fakes, contract tests and fixtures. Nothing selects, assembles, decides or calls a network; the router behaves exactly as today.

## Scope

| In | Out |
| --- | --- |
| Projects `decision/`, `assemble/`, `selector/` (packages `agent_pm.decision`, `agent_pm.assemble`, `agent_pm.selector`): types, reason codes, errors, ABCs | Any implementation: rule exclusion, ranking, fallback, attribute table loading (TASK-743); model and script assemblers (TASK-745); Jev and Kev deciders (decision backend issue) |
| `agent_pm.utils.Endpoint`, the HTTP endpoint shape both API layers configure; config records `DecisionConfig`, `AssembleConfig`, `SelectorConfig`; the construction signatures (Design › Construction contract) | Endpoint validation and key reading (TASK-745, signatures fixed here); factories and loaders (owners in the Construction contract) |
| Orchestrator records of the router↔selector call: `TierTable`, `RunChoice`, `SelectorConfig`, `gate.THRESHOLDS` | Building candidates, pin and usage conversion, tier mapping, `LinearRun` effort, selection log, `[selector]` config keys (TASK-747); CLI `agent-pm-select` and its JSON (CLI issue, FR-19 shape) |
| Import-linter contracts, repository guards, CI/PYTHONPATH/bin wiring for the three projects | `select-model` skill, manage skill text |
| Fakes (`FakeDecider`, `FakeAssembler`, `FakeSelector`), contract mixins (`DeciderContract`, `AssemblerContract`), fixtures | |
| Interface snapshots of the three packages; docs | |

Constraints:
- `tests/session_interface.json` and `tests/delegate_interface.json` stay byte-identical to `main`; no session or delegate source changes (FR-30). If one becomes necessary: stop and report it.
- No compatibility code. No real keychain, Security API or credentials in tests.
- No string `tier` or `linear` (case-insensitive) anywhere under `decision/`, `assemble/`, `selector/` (FR-26), and no client name (`claude`, `codex`, `opencode`) in their `src` (`tests/guard_checks/ownership.py` `client_names`).

## Terms

- **Generic layers**: the three packages `agent_pm.selector`, `agent_pm.assemble`, `agent_pm.decision`.
- **Candidate**: one (model, effort) pair the selector may pick.
- **Provider**: what usage is keyed by; the orchestrator passes its client name.
- **Reason code**: a short fixed string (a `StrEnum` value) explaining an exclusion or a fallback; never a request header, response body, secret or task text.

## Acceptance criteria

- AC1 (FR-26): `lint-imports --no-cache` passes and holds: the Production layers contract lists, top to bottom, `agent_pm.manager`, `agent_pm.linear_orchestrator`, `agent_pm.selector`, `agent_pm.assemble`, `agent_pm.decision`, `agent_pm.delegate`, `agent_pm.session`, `agent_pm.utils`; a units contract per generic layer allows `selector -> assemble decision utils`, `assemble -> utils`, `decision -> utils` and nothing else besides the standard library; an `independence` contract over `agent_pm.assemble` and `agent_pm.decision`; manager, delegate, session and utils are forbidden from importing any generic layer.
- AC2 (FR-26): `git grep -n -i "tier\|linear" -- decision assemble selector` prints nothing; a guard `generic_terms` in `tests/guard_checks/ownership.py` fails on any such match under those three projects and passes on this tree.
- AC3 (FR-26): no file under the three projects imports `agent_pm.manager`, `agent_pm.linear_orchestrator`, `agent_pm.delegate` or `agent_pm.session` (enforced by AC1's units contracts).
- AC4 (FR-27): `decision.Decider` is an ABC whose only abstract method is `decide(state, questions) -> tuple[Answer, ...]`; `FakeDecider` passes `DeciderContract`, which asserts one answer per question in order, `choice` among that question's option ids, `probabilities` keyed by exactly those ids each in [0, 1] summing to 1 within 1e-6, `confidence` in [0, 1], a non-model question (pick one of three roles) answered, and calls with the same input giving equal answers (stateless).
- AC5 (FR-27): `DecisionError(DecisionFailure.TIMEOUT)` carries `reason` and its `str()` is exactly the code (`"timeout"`); `DecisionFailure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC6 (FR-29): `assemble.Assembler` is an ABC whose only abstract method is `assemble(task) -> Assembly`; `FakeAssembler` passes `AssemblerContract`, which asserts a non-empty `state` for each of `TextInput`, `RefInput`, `FeaturesInput`, and that `reason` is `None` or an `AssembleFailure`; `AssembleFailure` values are exactly `error`, `timeout`, `no-key`, `invalid`.
- AC7 (FR-29): `agent_pm.assemble` and `agent_pm.decision` import no `agent_pm.selector` and need no `Model` (decide and assemble take no attribute table).
- AC8 (FR-39): `Selector` is an ABC whose only abstract method is `select(task, candidates, usage=None, pin=None, thresholds=None) -> Selection` (`pin=None`: nothing pinned), `task` a `TaskInput`; `FakeSelector` returns its scripted `Selection`, records each call's arguments, raises a scripted exception when given one, and raises `ValueError` at construction unless exactly one of the two is given.
- AC15 (construction): `DecisionConfig`, `Backend` (`jev`, `kev`), `AssembleConfig`, `ModelTable`, `selector.ConfigError` (whose `str()` contains the model id and field) and `linear_orchestrator.engine.types.SelectorConfig` exist with the Design fields; `repr(Endpoint(..., key_command=("/usr/bin/x", "s")))` does not contain `key_command`'s values; `Decider.name` is abstract (a subclass without it cannot be instantiated).
- AC9 (FR-39, FR-19, FR-24): `Selection` expresses a pick (`pick` a `Candidate`, `ranking` of `Ranked` with `score`, `excluded` of `Exclusion`, `decided_by`, `input`), and an all-excluded result (`pick=None`, `ranking=()`, `decided_by=None`, `excluded` non-empty); `ExclusionReason` values are exactly `disabled`, `effort`, `context`, `usage`; `DecidedBy` values are exactly `fallback`, `pinned`, `single`, `disabled`; `InputKind` values are exactly `text`, `ref`, `features`, and each task input's `kind` returns its own.
- AC10 (FR-39): `Model("acme", "m1", …).id == "acme/m1"`; `Usage` maps a provider to `ProviderUsage(five_hour, weekly)` of `Window(used_percent, resets_at)`; `Pin()` has both fields `None`.
- AC11 (router contract): `gate.THRESHOLDS == selector.Thresholds(five_hour=gate.MAX_5H, weekly=gate.MAX_WEEK)`; `TierTable` and `RunChoice` exist in `linear_orchestrator.engine.types` with the fields in Design; router behavior is unchanged (no router, `LinearRun` or config change; existing orchestrator unit tests pass).
- AC12 (interface artifact): `python3 -m tests.checks` passes, including a new `generic_interface_snapshot` whose files `tests/selector_interface.json`, `tests/assemble_interface.json`, `tests/decision_interface.json` equal the packages' exports; changing any export makes it report the drift.
- AC13 (FR-30): `session_interface_snapshot` and `delegate_interface_snapshot` pass; `git diff origin/main -- tests/session_interface.json tests/delegate_interface.json session/ delegate/` is empty.
- AC14 (wiring): ruff, pyright, vulture, pylint, `python3 -m tests.unit_imports`, `python3 .github/integration_jobs.py --check`, typos and every unit suite pass; `bin/agent-pm-linear` imports `agent_pm.linear_orchestrator` with the new roots on its `PYTHONPATH`; the changed `src` lines meet `.github/coverage_gate.py`.

## Design

### Data shapes

All records are `@dataclass(frozen=True)` in each package's `engine/types.py` (types-only: no public functions; properties only use `self`). Each package `__init__.py` re-exports them with a literal `__all__` and is listed in `OBJECT_EXPORT_MODULES`.

```python
# agent_pm.utils (utils/src/agent_pm/utils/endpoint.py, re-exported)
@dataclass(frozen=True)
class Endpoint:            # a text-only HTTP model API
    url: str
    model: str
    timeout: float                      # seconds
    key_command: tuple[str, ...] = field(default=(), repr=False)   # argv run without a shell; () → no key

# agent_pm.decision
class DecisionFailure(StrEnum): ERROR="error"; TIMEOUT="timeout"; NO_KEY="no-key"; INVALID="invalid"
class DecisionError(Exception):         # .reason: DecisionFailure; str() is the code only
    def __init__(self, reason: DecisionFailure): ...
class Backend(StrEnum): JEV="jev"; KEV="kev"
class DecisionConfig: backend: Backend; endpoint: Endpoint          # [selector.decision]
class Option: id: str; text: str
class Question: id: str; prompt: str; options: tuple[Option, ...]  # options: 1 or more, unique ids
class Answer: question: str; choice: str; probabilities: Mapping[str, float]; confidence: float
class Decider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...           # its decided_by value; never a DecidedBy value
    @abstractmethod
    def decide(self, state: str, questions: Sequence[Question]) -> tuple[Answer, ...]: ...

# agent_pm.assemble
class InputKind(StrEnum): TEXT="text"; REF="ref"; FEATURES="features"
class TextInput: text: str                                   # kind → TEXT
class RefInput: ref: str; text: str                          # a path or issue id and the text read from it; kind → REF
class FeaturesInput: type: str = ""; difficulty: str = ""; context: int | None = None   # kind → FEATURES
TaskInput = TextInput | RefInput | FeaturesInput
class AssembleFailure(StrEnum): ERROR="error"; TIMEOUT="timeout"; NO_KEY="no-key"; INVALID="invalid"
class Assembly: state: str; reason: AssembleFailure | None = None   # reason set: the model failed and the script built state
class AssembleConfig: enabled: bool; endpoint: Endpoint            # [selector.assemble]
class Assembler(ABC):
    @abstractmethod
    def assemble(self, task: TaskInput) -> Assembly: ...

# agent_pm.selector
class Model: provider: str; name: str; efforts: tuple[str, ...]; default_effort: str; context: int
             price_in: float; price_out: float; capabilities: str; enabled: bool = True
             id → f"{provider}/{name}" (property); a table key splits at the first `/` (`acme/vendor/m:1` → provider `acme`, name `vendor/m:1`), since model names may hold `/`
ModelTable = Mapping[str, Model]             # the attribute table, keyed by Model.id
class ConfigError(ValueError):               # a bad attribute-table entry; str() names the model id and field
    def __init__(self, model: str, field: str): ...   # .model, .field
class Candidate: model: Model; effort: str
class Candidates: options: tuple[Candidate, ...]; default: Candidate | None = None
class Window: used_percent: float; resets_at: float | None
class ProviderUsage: five_hour: Window | None; weekly: Window | None
Usage = Mapping[str, ProviderUsage]          # provider → its usage
class Thresholds: five_hour: float; weekly: float   # used_percent at or above → exhausted
class Pin: model: str | None = None; effort: str | None = None   # model: a Model.id
class ExclusionReason(StrEnum): DISABLED="disabled"; EFFORT="effort"; CONTEXT="context"; USAGE="usage"
class Exclusion: model: str; reason: ExclusionReason   # model: a Model.id
class Ranked: candidate: Candidate; score: float
class DecidedBy(StrEnum): FALLBACK="fallback"; PINNED="pinned"; SINGLE="single"; DISABLED="disabled"
class Selection: pick: Candidate | None; ranking: tuple[Ranked, ...]; excluded: tuple[Exclusion, ...]
                 decided_by: str | None; input: InputKind
                 assemble_reason: AssembleFailure | None = None; decision_reason: DecisionFailure | None = None
class Selector(ABC):
    @abstractmethod
    def select(self, task: TaskInput, candidates: Candidates, usage: Usage | None = None,
               pin: Pin | None = None, thresholds: Thresholds | None = None) -> Selection: ...
        # pin None: nothing pinned (ruff B008 rules out a Pin() default)
```

`agent_pm.selector` does not re-export assemble or decision names; callers import each from its own package.

### Contracts each docstring states (no code enforces them here)

- **Untrusted text**: task text, `TaskInput`, `state` and API responses are untrusted data. Implementations send `state` only as delimited data that cannot change questions or options; the control on an injected answer is the option-id check (`INVALID`), so injection can at most steer among the given options.
- **Error convention**: a runtime failure raises `DecisionError(code)` (decision) or becomes `Assembly.reason` (assemble), raised `from None`, with no message beyond the code; implementations never log, store or embed in an error the key, `key_command` output, task text, `state`, prompts, request headers, response bodies or an `Endpoint.url` (NFR-2, NFR-4). A bad configuration raises the package's `ConfigError` (selector) or `ValueError` naming the field (endpoint), before any request. Implementations do not follow HTTP redirects; a redirect is `ERROR`.
- `Endpoint`: `key_command` names a lookup and never holds the secret; the key is read per request, kept only in a local variable, and never stored on any record. The composition root, `build_selector` (`linear_orchestrator.engine.selector_root`, which the CLI reuses), calls `check_endpoint` on every endpoint before any factory call; its `ValueError` names the field only, never the value.
- `Decider.decide`: stateless; starts no process or session, calls no tool; one `Answer` per question in order; `choice` and `probabilities` keys are that question's option ids; a question with no options or a reply outside them raises `DecisionError(INVALID)`; other failures raise `DecisionError` with their code; `name` is what `Selection.decided_by` records and is never a `DecidedBy` value.
- `Assembler.assemble`: returns the `state` the decider reads (short English task features, or the task input itself when built by script); never raises for a model failure: it falls back to the script and sets `reason`.
- `Selector.select` (FR-39, FR-13, FR-16, FR-18): implementations take an `Assembler` and a `Decider` in their constructor; `candidates.options` are in the caller's order; it excludes by rule first (each `Exclusion` names a model, all its efforts excluded), calls `assemble` only when 2 or more candidates remain, then `decide` once; `usage=None` or `thresholds=None` → no `usage` exclusion; **pin** (FR-7, FR-13, FR-39): `pin.model` names a model among `candidates.options` (else `ValueError` before any call: a caller error, as the orchestrator builds both from one table) and drops every other model's candidates, which are not exclusions and get no `Exclusion`; `pin.effort` keeps only candidates of that effort, and a remaining model with no such candidate (its `efforts` lacks it) is excluded with `EFFORT`; rule exclusion then runs on what is left, so `pick=None` always has a non-empty `excluded`; all excluded → `pick=None`, `ranking=()`, `decided_by=None`; one left → no assemble or decide call, `decided_by=PINNED` when both `pin.model` and `pin.effort` are set, else `SINGLE`; ranking by probability → `decided_by=decider.name`; failure → `FALLBACK` and `decision_reason`. `DISABLED` is set only by callers that do not select (CLI with `[selector] enabled = false`).

### Construction contract (signatures the follow-up issues add; fixed here, not coded)

| Signature | Package | Adds it | Callers |
| --- | --- | --- | --- |
| `load_models(raw: Mapping[str, Mapping[str, object]]) -> ModelTable`, raising `ConfigError` (FR-1, FR-4 field checks) | `agent_pm.selector` | TASK-743 | TASK-747, CLI |
| `selector(assembler: Assembler, decider: Decider, *, max_options: int = 20) -> Selector` | `agent_pm.selector` | TASK-743 | TASK-747, CLI |
| `assembler(config: AssembleConfig, *, read_key: Callable[[Endpoint], str]) -> Assembler` (`enabled=False` → the script assembler) | `agent_pm.assemble` | TASK-745 | TASK-747, CLI |
| `decider(config: DecisionConfig, *, read_key: Callable[[Endpoint], str]) -> Decider` | `agent_pm.decision` | decision backend issue | TASK-747, CLI |
| `check_endpoint(endpoint: Endpoint) -> None` raising `ValueError` naming the field (scheme exactly `https`, or `http` only when the parsed hostname is loopback, any other scheme (`file`, `ftp`, …) rejected naming `url`; loopback is exactly `localhost`, or a literal IP in `127.0.0.0/8` or `::1`, never a prefix or substring match; no userinfo or query in `url`, absolute `key_command[0]`); `read_key(endpoint: Endpoint) -> str` raising `KeyError` `from None` with no detail and no command output when the command fails | `agent_pm.utils` | TASK-745 | the orchestrator, which passes `read_key` to `assembler` and `decider` |
| `tier_table(client: str, *, local: str = utils.LOCAL) -> TierTable` (FR-31): reads session's packaged config as a resource by name, `utils.read_resources((("agent_pm.session", "config.toml"),), local=local)`, the `[session]` section of `local` merged over it, and takes `clients.<client>.tiers` and `clients.<client>.efforts`; no import of `agent_pm.session` and no delegate interface change (FR-30; the same resource read `delegate/src/agent_pm/delegate/engine/config.py` `read_config` does); an unknown client or a missing table raises `linear_orchestrator`'s `ConfigError` naming the client and table. Its test compares the result with session's own parse (orchestrator tests may import `agent_pm.session`) | `linear_orchestrator.engine.selection` | TASK-747 | router, CLI |
| `selector_config(raw: Mapping[str, object], *, load_models: Callable[[Mapping[str, Mapping[str, object]]], ModelTable]) -> SelectorConfig` (the merged `[selector]` section; FR-4 cross-check; `load_models` injected so it does not wait on TASK-743); every failure, a wrapped `selector.ConfigError` included, raises `linear_orchestrator`'s `ConfigError` (exit 2 in the CLI, `config-error` in the router) whose message names the field, never its value (NFR-2: a `url` or `key_command` may hold a credential); it does not call `check_endpoint` | `linear_orchestrator.engine.selection` | TASK-747 | router, CLI |
| `build_selector(config: SelectorConfig, *, read_key: Callable[[Endpoint], str] = utils.read_key) -> Selector` (the one injection point and the only `check_endpoint` caller: `check_endpoint` on both endpoints, its `ValueError` re-raised `from None` as `linear_orchestrator`'s `ConfigError` naming the field only, then `assembler`, `decider`, `selector`); in its own module so `engine.selection` imports without the factories | `linear_orchestrator.engine.selector_root` | TASK-747, last (after TASK-743, TASK-745 and the decision backend issue merge) | router, CLI |
| `candidates(table: TierTable, models: ModelTable, default: tuple[int, str] \| None) -> Candidates` (FR-31; `default` the run's tier and effort; a tier model with no `ModelTable` entry raises `ConfigError` naming the model (FR-4); a `default` whose model or effort is not among the options gives `default=None`) | `linear_orchestrator.engine.selection` | TASK-747 | router, CLI |
| `tier_pin(table: TierTable, tier: int \| None, effort: str \| None) -> Pin` (FR-7, FR-39) | `linear_orchestrator.engine.selection` | TASK-747 | router, CLI |
| `pick_tier(table: TierTable, pick: Candidate, run_tier: int \| None) -> int` (FR-32) | `linear_orchestrator.engine.selection` | TASK-747 | router, CLI |
| `provider_usage(client: str, usage: object \| None) -> Usage \| None` (FR-12; `usage` is what `Delegate.usage` returns, read by its `five_hour`/`weekly` windows' `used_percent` and `resets_at`: `agent_pm.delegate` exports no `Usage` type and the orchestrator may not import session) | `linear_orchestrator.engine.selection` | TASK-747 | router |

Test rule for every follow-up issue: tests always inject a fake `read_key` (and never `utils.read_key`'s default) and never run a shipped `key_command` such as `/usr/bin/security …`, so no test reaches the keychain or real credentials. `read_key` is injected so the API layers never wait on each other: TASK-745, the decision backend issue and their tests pass a fake `read_key`. TASK-747 builds the `Selector` at one injection point (from `SelectorConfig` and the factories) and tests the router with `FakeSelector`, so its router work does not wait on the factory owners; the CLI does the same.

The raw attribute table lives in linear_orchestrator configuration (`[selector.models."<client>/<model>"]`, local overrides merged by `utils` config loading); the orchestrator passes it to `load_models`. The whole linear-orchestrator `config.toml` `[selector]` section (its keys, the shipped `[selector.models."<client>/<model>"]` entries of FR-1) and any `client_names`/`orchestrator_text` guard exception they need are TASK-747's, one owner; TASK-743 owns only `load_models` and tests it with its own raw tables.

### decision/, assemble/, selector/ (new projects)

Each: `src/agent_pm/<pkg>/__init__.py` (docstring, re-exports, `__all__`), `src/agent_pm/<pkg>/engine/__init__.py` (docstring), `src/agent_pm/<pkg>/engine/types.py`; no `src/agent_pm/__init__.py`; `tests/__init__.py`, `tests/unit/__init__.py`, plus:
- `decision/tests/fakes/decider.py` `FakeDecider(Decider)`: `__init__(self, probabilities: Mapping[str, Mapping[str, float]] | None = None, *, error: DecisionError | None = None, name: str = "fake")`; per question uses `probabilities[question.id]`, else uniform over its options; `choice` the highest (first on ties), `confidence` that probability; records `calls: list[tuple[str, tuple[Question, ...]]]`; raises `error` when given; raises `DecisionError(INVALID)` for a question with no options or scripted probabilities whose keys differ from its option ids.
- `decision/tests/contracts/decider.py` `DeciderContract`: a mixin over `Case` with hook `make() -> Decider`; the AC4 cases, plus: a question with no options raises `DecisionError` whose `reason` is `INVALID`; `name` is non-empty and not a `DecidedBy` value (spelled as the four literals, since decision may not import selector).
- `assemble/tests/fakes/assembler.py` `FakeAssembler(Assembler)`: `__init__(self, state: str | None = None, *, reason: AssembleFailure | None = None)`; returns `Assembly(state or <the task's text when non-blank, else "empty task", else its features as "type=…, difficulty=…, context=…">, reason)`; records `calls: list[TaskInput]`.
- `assemble/tests/contracts/assembler.py` `AssemblerContract`: hook `make() -> Assembler`; the AC6 cases.
- `selector/tests/fakes/selector.py` `FakeSelector(Selector)`: `__init__(self, selection: Selection | None = None, *, error: Exception | None = None)`; exactly one of the two is given, else `ValueError` at construction; records `calls: list[dict]` of the five arguments by name.
- `selector/tests/fixtures.py`: `model(name, **overrides) -> Model` (provider `acme`, efforts `("low", "high")`, default `"low"`, context 200000, prices 1.0/5.0), `MODELS` (three models `small`, `mid`, `big` with rising prices and contexts), `candidates(*models, default=None) -> Candidates` (each model × each effort, models' order), `USAGE` (`{"acme": ProviderUsage(Window(10.0, None), Window(20.0, None))}`), `THRESHOLDS = Thresholds(90, 100)`, `PICKED` (a `Selection` decided by `"fake"`), `NONE_LEFT` (all excluded, one per `ExclusionReason`).
- Unit tests per AC (Verification).

### utils/src/agent_pm/utils/endpoint.py

`Endpoint` as above; docstring: the endpoint shape the API layers configure; validation lives here when added.

### linear-orchestrator

- `engine/gate.py`: `THRESHOLDS = selector.Thresholds(five_hour=MAX_5H, weekly=MAX_WEEK)`, comment: the thresholds the router passes to `Selector.select` (FR-39).
- `engine/types.py`:
  - `TierTable(client: str, models: Mapping[int, str], efforts: tuple[str, ...])`: a client's `[clients.<client>.tiers]` (tier → model name) and its `[clients.<client>.efforts]` names, as the session config gives them with local overrides. Docstring states the conversion contract TASK-747 implements: candidates are its models deduplicated in tier order, each crossed with `efforts` ∩ `Model.efforts`, `Model.provider` = `client`; a tier pin becomes `Pin(model=f"{client}/{models[tier]}")`; a pick maps back to the run's own tier when that tier names the picked model, else the lowest tier that does (FR-31, FR-32).
  - `SelectorConfig(enabled: bool, pin_roles: tuple[str, ...], max_options: int, models: selector.ModelTable, assemble: assemble.AssembleConfig, decision: decision.DecisionConfig)`: the `[selector]` section (FR-7, FR-8, FR-16, FR-28, FR-33); its parsing is TASK-747's.
  - `RunChoice(tier: int, effort: str, selection: selector.Selection)`: what a new run launches with after a selection that picked (`selection.pick` not `None`); the router logs `issue`, `pick` (model, `tier`, `effort`), `decided_by` and the `excluded` reason codes from it (FR-37). A `Selection` with `pick=None` never becomes a `RunChoice`: the router logs `launch-error` with `no model` and those codes from the `Selection` itself (FR-15).
- The router passes `TextInput(issue text)`, `Candidates` from `TierTable`, the gate's usage as `{client: ProviderUsage(...)}` (None with `--issue`), the pin, and `THRESHOLDS`.
- Router logging (NFR-4, `RunChoice` docstring): the selection line and a `launch-error` from selection carry only the FR-37 fields (issue, pick model, `tier`, `effort`, `decided_by`, reason codes); never the `TaskInput`, issue text, `Assembly.state` or an exception's `str()`. An exception raised by `select` (a bug: failures inside it become `FALLBACK`) → `launch-error` with `no model` and `error` the exception's class name only; the run does not start.

### Repository wiring

- `pyproject.toml`: pyright `include`/`extraPaths` and vulture `paths` add `decision/src`, `assemble/src`, `selector/src`; `root_packages` add `decision.tests`, `assemble.tests`, `selector.tests`; contracts per AC1; "Manager communicates…" forbids the three; "linear_orchestrator uses lower public packages" forbids `agent_pm.selector.*`, `agent_pm.assemble.*`, `agent_pm.decision.*`; units contract of linear_orchestrator adds `agent_pm.selector agent_pm.assemble agent_pm.decision` to the `engine`, `engine.types` and `cli` dependency lines; one "<pkg> tests respect layer ownership" forbidden contract per new test package (forbids `tests`, manager, linear_orchestrator, delegate, session and their test packages; `assemble.tests` and `decision.tests` also each other); the existing lower test-ownership contracts add the three packages.
- `tests/guards.py` `LEVELS = (("utils", "session"), ("delegate",), ("decision", "assemble"), ("selector",), ("linear-orchestrator",), ("manager",))`; `tests/unit/guards_test.py` order follows.
- `tests/guard_data/{decision,assemble,selector}.py`: `OBJECT_EXPORT_MODULES`, the interface snapshot path and modules; `tests/unit/guard_data_test.py` order follows.
- `tests/guard_checks/imports.py` `orchestrator_imports`: linear_orchestrator may also import the generic layers' `__all__` names.
- `tests/guard_checks/exports.py`: `generic_interface_snapshot(repo)` reusing `_snapshot_drift` for each package; docstring: TASK-742 and the TASK-511 issues building on it change it on purpose.
- `tests/guard_checks/ownership.py`: `generic_terms(repo)` (AC2).
- PYTHONPATH and src lists: `.github/workflows/test.yml` (env, `chmod -R a-w` lines, pylint), `docs/tests.md`, `bin/agent-pm-linear` (adds `decision/src:assemble/src:selector/src`), `linear-orchestrator/tests/orchestrator_tests/integration/linear_bin_integration_test.py`, `linear-orchestrator/tests/orchestrator_tests/integration/config_isolation_integration_test.py` `PackagedDeployments`.
- `.github/vulture_whitelist.py`: each name only tests use, one entry per name or group, its comment naming the ticket that calls it (TASK-743, TASK-745, TASK-747).

### Docs

- `docs/architecture.md`: layer count and list; a section "Model selection layers": the three generic layers, who imports whom, the router's `select` call (inputs from `TierTable`, gate usage, pin, `THRESHOLDS`; output `RunChoice`), reason codes, error convention, where fakes and contracts live.
- `CONTEXT.md`: layer count; terms Selector, Assemble layer, Decision layer, Candidate.
- `AGENTS.md`: layer order (Import rules) and "the eight `src` trees".

## Verification

Tests come first; all hermetic `unit` tests using `utils.tests.case.Case` (no subprocess, network or keychain).

| AC | Test or check |
| --- | --- |
| AC1 | `lint-imports --no-cache` |
| AC2 | `git grep -n -i "tier\|linear" -- decision assemble selector`; `tests/unit/guard_checks/ownership_test.py` `generic_terms` cases (a match fails, this tree passes) or the existing guard sample mechanism |
| AC3 | `lint-imports --no-cache` |
| AC4 | `decision/tests/unit/fakes/decider_test.py` (`FakeDecider` × `DeciderContract`) |
| AC5 | `decision/tests/unit/engine/types_test.py` |
| AC6 | `assemble/tests/unit/fakes/assembler_test.py` (`FakeAssembler` × `AssemblerContract`); `assemble/tests/unit/engine/types_test.py` |
| AC7 | `lint-imports --no-cache` (units contracts) |
| AC8 | `selector/tests/unit/fakes/selector_test.py` |
| AC9 | `selector/tests/unit/engine/types_test.py`; `assemble/tests/unit/engine/types_test.py` (`InputKind`, `kind`) |
| AC10 | `selector/tests/unit/engine/types_test.py` |
| AC11 | `linear-orchestrator/tests/orchestrator_tests/unit/engine/gate_test.py` `THRESHOLDS` case; `linear-orchestrator/tests/orchestrator_tests/unit/engine/types_test.py` `TierTable`/`RunChoice` construction; Orchestrator unit suite |
| AC12 | `python3 -m tests.checks`; a unit case in `tests/unit/` calling `generic_interface_snapshot` on a temp copy with a changed snapshot reports drift |
| AC13 | `python3 -m tests.checks`; `git diff origin/main --stat -- tests/session_interface.json tests/delegate_interface.json session delegate` |
| AC14 | the lint commands of `docs/tests.md`; `python3 .github/run_tests.py Unit --unit`; CI `test.yml` (integration, coverage gate) |
| AC15 | `decision/tests/unit/engine/types_test.py`, `assemble/tests/unit/engine/types_test.py`, `selector/tests/unit/engine/types_test.py`, `utils/tests/unit/endpoint_test.py`, `linear-orchestrator/tests/orchestrator_tests/unit/engine/types_test.py` |

Every suite the repo names (`docs/tests.md`, `.github/workflows/test.yml`) passes.

## Decisions

- D1: Three top-level projects, not subpackages: `utils/tests/sandbox.py` `project_roots` and `placement.py` `namespace_package` allow one package per project.
- D2: Abstract interfaces are ABCs (repo norm), not `Protocol`; no stub functions, so there is no behavior and nothing for coverage or vulture to miss beyond whitelisted names.
- D3: `select` is `Selector.select`, an abstract method, so the FR-39 entry is fixed without a stub; TASK-743's concrete class takes the assembler and decider in its constructor, which lets the FR-39 test pass a fake assembler.
- D4: Task inputs and `InputKind` live in `agent_pm.assemble` (the layer that reads tasks); the selector imports them; decision takes only `state: str`.
- D5: `decided_by` is `str | None`: a `DecidedBy` code or the decider's `name` (`jev`, `kev`, or a fake's), so the selector never names a backend; `None` only when nothing is left.
- D6: Fallback reasons are two typed fields (`assemble_reason`, `decision_reason`) rather than a free list.
- D7: Usage is keyed by provider and named `provider` in the generic layers; the orchestrator passes the client name (`TierTable.client`).
- D8: `Endpoint` goes in utils because both API layers configure the same shape and may not import each other.
- D9: The import contracts place the generic layers between linear_orchestrator and delegate in the single Production layers contract (`.github/ci_scope.py` accepts one), with units, independence and forbidden contracts for the rest.
- D10: Interface snapshots make later drift visible; follow-up issues update them on purpose.
- D11: The orchestrator gets records here; its conversion and parsing functions are fixed as signatures in `linear_orchestrator.engine.selection` (Construction contract), written by TASK-747 and reused by the CLI issue, which tests against the signatures with its own fakes until TASK-747 merges.
- D12: Factories, loaders and endpoint helpers are fixed as signatures with one named owner each (Construction contract table), not stubs, so no behavior ships and parallel issues neither wait nor collide; `ModelTable`, `ConfigError`, the config records and `SelectorConfig` ship as types.
- D15: `read_key` is a constructor argument of `assembler`/`decider`, not an import, so the decision backend issue does not wait on TASK-745 (architecture#6).
- D13: AC2's grep is a plain case-insensitive substring match as FR-26 writes it; authors avoid words containing `tier` or `linear` (e.g. "frontier", "nonlinear").
- D14: `Selection` does not validate its invariants (types-only modules hold no behavior); TASK-743's tests own them, `fixtures.NONE_LEFT` and `PICKED` show the valid shapes.
- D16: TASK-747's `tier_table` reads session's `config.toml` as a package resource by name (FR-31: no import of session), as `delegate` `read_config` does, instead of a delegate export, so FR-30's frozen delegate interface stays unchanged; the coupling to session's config format is the PRD's accepted risk.
- Left to the implementer: docstring wording, fixture values, exact vulture whitelist grouping, test case names.
