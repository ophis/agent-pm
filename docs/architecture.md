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
    SL["engine/selection.py<br>translate · Resolver · TierResolver"]
  end
  subgraph SR[agent_pm.selector]
    SS["engine/: Selector · SelectorConfig · SelectorError"]
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

Before a new run, the router can decide its launch choices from the issue (TASK-511), in `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/selection.py`: `translate` runs the `choice-translator` role once through the configured Delegate and returns the issue's state text; then each step of the chain, a `Resolver` paired with its `Choice`, lists its own options from config for the `Context` so far and asks a `Selector` (`agent_pm.selector`, a decision API such as Jev or Kev) to pick one. A step switched off, or failing with `SelectionError`, uses its `fallback`, today's choice. The router applies each `Choice` to the `Context` and records it on the run's start row. `TierResolver` picks the tier and effort among the client's `[clients.<client>.tiers]` and `efforts` in session's merged config. `engine/chain.py` runs the chain before each new run: `[linear_orchestrator.selection]` switches `translate` and each step, all off by default ([board.md › Task labels](board.md#task-labels)); `translate` runs only when a step is on; the run launches with the final `Context` (`engine/types.py`), and a resume reuses its start row's recorded tier and effort. Until the Jev and Kev selectors come, the router's Selector is `NoSelector` (every pick fails with `no-key`); tests use `FakeSelector` (`selector/tests/fakes/`) and `FakeResolver` (`linear-orchestrator/tests/orchestrator_tests/selection_fixtures.py`).

```mermaid
classDiagram
  direction TB
  class Selector {
    <<abstract>>
    +select(state, choices) str
  }
  class FakeSelector {
    <<test fake>>
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
  class TierChoice {
    tier
    effort
  }
  class TierResolver {
    selector
    task_tiers
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
    +translate(issue, workdir, provider, overlays) str
  }
  Selector <|-- FakeSelector
  Choice <|-- TierChoice
  Resolver <|-- TierResolver
  Resolver <|-- FakeResolver
  TierResolver ..> TierChoice : creates
  TierResolver ..> Selector : select
  Choice ..> Context : apply
```

Dashed arrow: a call, use or creation; `<|--`: implements. `translate` runs the role through `agent_pm.delegate` (`Delegate.create` and `run`, as `LinearRun` does); `TierResolver` reads session's merged config by resource name, never importing session.
