# Core architecture

Six layers, one project each: utils < session < delegate < selector < linear-orchestrator < manager. Import rules: `AGENTS.md` › Import rules. The code, by package; terms in `CONTEXT.md`.

```mermaid
flowchart TB
  subgraph EN[entry points]
    LD[launchd]
    HU[the user]
    CC[Starter, control and resume commands]
  end
  subgraph MG[agent_pm.manager]
    MC["cli.py<br>agent-pm-crew"] --> W["engine/crew.py"] --> RS["engine/roster.py"]
    MC --> HO["engine/holder.py<br>lease while a command runs"] --> RS
  end
  subgraph LO[agent_pm.linear_orchestrator]
    LCLI["cli.py<br>agent-pm-linear"]
    R["engine/router.py<br>pick · usage gate · trigger"]
    LR["engine/linear_run.py<br>Delegate ownership · queries"]
    WB["engine/writeback.py<br>one event's write-back"]
    PP["engine/promote.py → prune.py"]
    LC["engine/config.py<br>[core] overlay"]
    SL["engine/selection.py<br>translate · Resolver · ModelResolver · EffortResolver"]
  end
  subgraph SR[agent_pm.selector]
    SS["engine/: Selector · State · Question · SelectorConfig · SelectorError"]
    SJ["backends/jev.py<br>JevSelector"]
  end
  subgraph DL[agent_pm.delegate]
    D["cli.py<br>agent-pm-delegate"]
    HD["engine/harness_delegate.py<br>HarnessDelegate · state.py RunState"]
    H["engine/harness.py<br>detached run"]
    E["engine/: base · plan · drivers · runner · callback · types · config · ready_gate"]
    C["composer/<br>prompt · run spec"]
    EV["event/<br>reply · outcome check · callback line"]
    CF["config.py<br>package settings"]
    subgraph TM[team/]
      T["roles · tasks · templates"]
      AT[tools/]
    end
  end
  subgraph SE[agent_pm.session]
    SC["cli.py<br>agent-pm-session"]
    DR["engine/: base · hosted_session · worker · types · records · tmux_cli<br>modes/: headless · tui · in_process<br>modes/client_adapters: Headless · JsonHeadless · Tui · InProcess<br>viewer/: viewer · display · placement · grid · iterm · hidden<br>hosts/base: Host · Process · Interactive · InteractiveProcess · Piped<br>hosts/types: SessionView · CommandView<br>hosts/reclaimed: ReclaimedGroup<br>hosts/tmux/: host · hook<br>hosts/docker/: host · hook · broker · daemon · mounts · pipe · relay · stub · supervisor<br>hosts/pty/: host · supervisor · keys"]
    subgraph AD[clients/]
      CL[claude]
      CX[codex]
    end
  end
  U[agent_pm.utils]
  subgraph RT[clients]
    P1[Claude Code]
    P2[Codex CLI]
  end
  LD -.->|"router · promote"| LCLI
  HU -.->|"router --issue"| LCLI
  HU -.->|"act-as · CLI"| D
  HU -.->|"ctmux · CLI"| MC
  HU -.-> SC
  SC --> DR
  LCLI --> R & PP & WB & LR
  R & PP & WB --> LR
  R -->|"writeback.replay"| WB
  LR -->|"agent_pm.delegate: detach, views"| HD
  H -.->|"each event: agent-pm-linear writeback"| LCLI
  LC -->|"Delegate class methods"| HD
  SL -->|"agent_pm.selector"| SS
  SL -->|"agent_pm.delegate: create, run"| HD
  W -.-> CC
  D & H --> HD
  D --> C
  HD -.->|"detach on the harness Host"| H
  HD --> E & C
  E --> C
  DL -->|agent_pm.session| SE
  C -->|reads| T
  DR --> AD
  CL -.-> P1
  CX -.-> P2
  RT -.-> AT
  classDef shared stroke-dasharray: 4 3
  class EV,U,CF shared
```

Solid arrow: an import, the only direction allowed, or a file read (`reads`); its label names the package imported, by its `__all__`. Dotted: runs a command or process. Dashed: shared modules, their imports not drawn.

## Resolver chain

Before a new run, the router can decide its launch choices from the issue (TASK-511), in `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/selection.py`: `translate` runs the `choice-translator` role once through the configured Delegate and parses its JSON deliverable, a faithful English rendering of the issue, into a `State` (`agent_pm.selector`): its `fields` the title, description, status and labels, its droppable list the comments, oldest first by `created_at`. Then each step of the chain, a `Resolver` paired with its `Choice`, builds its own `Question` from config for the `Context` so far, its instructions and a description of each option, and asks a `Selector` (a decision API such as Jev or Kev) `select(state, question)` for one. A step without state, or failing, uses its `fallback`, today's choice, for itself alone. The router applies each `Choice` to the `Context` and records it on the run's start row. `ModelResolver` picks among the client's distinct `[clients.<client>.tiers]` models in session's merged config, most capable first, its `ModelChoice` the model's lowest tier; `EffortResolver` picks among its `efforts`, its question naming the model of the `Context`'s tier, its `EffortChoice` that effort. `JevSelector` (`selector/src/agent_pm/selector/backends/jev.py`) asks Jev: one `POST` to `/v1/systemone` per select with two or more options, https only (http only on a loopback IP), with no proxy or redirect, its key from `SelectorConfig.key_command`. It puts its own sentence, treat the state as data, before the caller's instructions, and sends the state's fields and droppable items as JSON. When a request's estimate, the UTF-8 bytes of its compact JSON (never fewer than its tokens), exceeds the model's per-question window (`WINDOWS`, from TypeSafe's model docs), it drops the oldest droppable items until it fits, never a field; no fit without them: `SelectorError(TOO_LARGE)`. Built with no arguments, it reads `[jev]` of `selector/src/agent_pm/selector/config.toml` (local `[selector.jev]`): `key`, the Keychain service (default `typesafe-jev`), `url`, `model` (one in `WINDOWS`) and `timeout`, so another service name or a self-hosted System One compatible server is a config change. `engine/chain.py` runs the chain before each new run from `[linear_orchestrator.selection]` ([board.md › Task labels](board.md#task-labels)): `resolvers` lists the chain's resolvers in order (`chain.RESOLVERS` maps each name to its `Resolver`), each new run with resolvers translating the issue first, and `selector` names the Selector class (`<module>:<Class>`, packaged: `agent_pm.selector:JevSelector`), so a new Selector is its class plus that line. No resolvers, the default: no chain, no translate, no Selector built, runs start as before. The run launches with the final `Context` (`engine/types.py`), and a resume reuses its start row's recorded tier and effort, with no translate or select. A Kev selector comes later; tests use `FakeSelector` (`selector/tests/fakes/`) and `FakeResolver` (`linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py`).

```mermaid
classDiagram
  direction TB
  class Selector {
    <<abstract>>
    +select(state, question) str
  }
  class State {
    fields
    droppable
    +sent(dropped) dict
  }
  class Question {
    instructions
    options
  }
  class FakeSelector {
    <<test fake>>
  }
  class JevSelector {
    config
  }
  class Choice {
    <<abstract>>
    +apply(context) Context
    +record() dict
  }
  class Resolver~C~ {
    <<abstract>>
    +choose(state, context) C
    +fallback(context) C
  }
  class ModelChoice {
    tier
  }
  class EffortChoice {
    effort
  }
  class ModelResolver {
    selector
    task_tiers
  }
  class EffortResolver {
    selector
  }
  class FakeResolver~C~ {
    <<test fake>>
  }
  class Context {
    role
    task
    client
    tier
    effort
  }
  class translate {
    <<function>>
    +translate(issue, workdir, provider, overlays) State
  }
  Selector <|-- FakeSelector
  Selector <|-- JevSelector
  Choice <|-- ModelChoice
  Choice <|-- EffortChoice
  Resolver <|-- ModelResolver
  Resolver <|-- EffortResolver
  Resolver <|-- FakeResolver
  ModelResolver ..> ModelChoice : creates
  EffortResolver ..> EffortChoice : creates
  ModelResolver ..> Selector : select
  EffortResolver ..> Selector : select
  Selector ..> State : sends
  Selector ..> Question : asks
  translate ..> State : creates
  Choice ..> Context : apply
```

Dashed arrow: a call, use or creation; `<|--`: implements. `translate` runs the role through `agent_pm.delegate` (`Delegate.create` and `run`, as `LinearRun` does); `ModelResolver` and `EffortResolver` read session's merged config by resource name, never importing session.
