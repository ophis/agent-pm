# <img src="../images/icon.jpg" alt="" width="40" align="absmiddle"> agent-pm

[English](../../README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · **Português**

*Esta é uma tradução; a versão de referência é o README em inglês.*

[![CI](https://github.com/ophis/agent-pm/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/ophis/agent-pm/actions/workflows/test.yml?query=branch%3Amain) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

Executa agentes de programação (Claude Code, Codex e outros no futuro) sem supervisão a partir de um quadro do Linear. Cada project do Linear é um produto, e o assignee de uma issue (uma conta de role) é a sua etapa. Cada role trabalha em até `max_runs` issues ao mesmo tempo e devolve o resultado para você revisar. Depois que você aprova, o próximo role assume.

| Camada | O que faz | Comece em |
|---|---|---|
| Session | Inicia, acompanha e retoma um agente de programação (Claude Code, Codex) no tmux ou num pty, da mesma forma para todo client. | [Sessions](../sessions.md) |
| Delegate (núcleo) | Executa um role numa task por meio de uma session e devolve um resultado verificado. Um plugin do Claude Code e do Codex que você pode usar sozinho. | [Instalação](#instalação) |
| Linear orchestrator | Executa os roles a partir de um quadro do Linear: o meu próprio fluxo de trabalho. | [Quadro do Linear](../board.md) |
| Manager | Um condutor para as camadas abaixo: um agente com quem você conversa, que lança workers, role runs e runs do quadro, acompanha-os e reporta. | [Demo](#demo-um-manager-vários-workers) |

As duas camadas de baixo, Session e Delegate, são blocos genéricos: use-os para montar o seu próprio fluxo. O Linear orchestrator é a minha filosofia de como os agentes devem trabalhar (pesquisa, depois um PRD, depois um build, com revisão humana entre cada etapa). Use como está, ignore ou altere; os roles e as tasks também são seus para editar ([Personalizar roles e tasks](../customizing-roles-and-tasks.md)).

Desenvolvedores: [`AGENTS.md`](../../AGENTS.md).

```mermaid
flowchart LR
    R["researcher<br>deep-research, research, light-research"] -->|"report → Approve → promote"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[agent-pm-linear router] -.->|claims Todo issues| R & P & E
```

## Por que agent-pm

![Trabalhar com agentes de programação: um agente, vários agentes à mão, agent-pm](../images/compare.svg)

Um único agente deixa você esperando; vários agentes à mão fazem cada prompt, verificação e aprovação passar por você. Com o agent-pm você conversa com um manager, que lança os workers, os acompanha e reporta.

## Demo: um manager, vários workers

Sem o quadro, você pode usar o agent-pm conversando com um único agente, o manager. Abra a session dele com `ctmux <name>`, inicie o Claude Code ou o Codex nela e invoque `/agent-pm:manage`. Você passa o trabalho em palavras simples; ele lança um worker por task, acompanha os eventos, verifica os pull requests e reporta.

![Uma session de manager (à esquerda) coordenando workers engineer numa grade do tmux](../images/demo.jpg)

- **Esquerda:** o manager, aqui o Claude Code. Ele lista quais issues estão em execução, aguardando ou não iniciadas, e reporta o que cada worker acabou de fazer.
- **Direita:** um painel por worker engineer, cada um construindo uma issue no seu próprio worktree. Os workers misturam clients: a maioria dos painéis é Claude Code (`auto mode on` embaixo), e o do meio à esquerda é Codex (`GPT-6.1-Sol xhigh`, `Ask Codex to do anything`). Os painéis preenchem uma grade conforme os workers iniciam e fecham quando o trabalho termina.

Comandos: [Início rápido](#início-rápido).

## Instalação

Cada client instala o plugin por conta própria. Antes de atualizar, veja [Upgrading](../upgrading.md).

| Client | Instalação |
|---|---|
| Claude Code | `/plugin marketplace add ophis/agent-pm`, depois `/plugin install agent-pm@agent-pm` |
| Codex 0.156.0+ | `codex plugin marketplace add ophis/agent-pm` (para atualizar um já adicionado: `codex plugin marketplace upgrade agent-pm`), depois `codex plugin add agent-pm@agent-pm`. `codex plugin list -m agent-pm` deve mostrá-lo instalado e habilitado. |

Nada disto é verificado. Se faltar algum, o run do agente falha.

| Requisito | Para |
|---|---|
| CLI `claude`, Python 3.11+ | todo run de agente |
| o plugin [`autopilot`](https://github.com/ophis/claude-autopilot), em cada client: Claude Code `/plugin marketplace add ophis/claude-autopilot`, depois `/plugin install autopilot@claude-autopilot`; Codex `codex plugin marketplace add ophis/claude-autopilot`, depois `codex plugin add autopilot@claude-autopilot` | programar: as build tasks do engineer (`/autopilot:build`, `/autopilot:light-build`). Desnecessário fora isso. |
| tmux 3.3+ | tui runs no Host `tmux` |
| `git`, `gh` autenticado | programar (checkouts, pull requests) e destinos no GitHub. Desnecessário fora isso. |
| iTerm2 em execução | a divisão padrão do iTerm2 |

Como as sessions que o agent-pm inicia carregam as suas configurações, e por que alguns comandos se recusam a rodar dentro delas: [Session settings](../sessions.md#session-settings).

| Comando | Faz |
|---|---|
| `ctmux <name>` | Abre a session tmux do commander: janelas nativas no iTerm2, um tmux attach simples em outros terminais, uma troca de session de dentro do tmux. `ctmux --install` coloca o ctmux e os quatro comandos `agent-pm-crew`, `agent-pm-session`, `agent-pm-delegate` e `agent-pm-linear` em `~/.agent-pm/bin/`: execute-os por esse caminho ou adicione-o ao `PATH`. Cada um executa o comando a partir do plugin instalado no momento da execução (primeiro a instalação do Claude Code, depois a mais recente do Codex), então uma atualização não exige reinstalar; rode `--install` de novo para atualizá-los. |
| `/agent-pm:manage` | Lança e dirige workers, role runs e pipeline runs, com as diretrizes do commander. |
| `/agent-pm:act-as <role>[:<task>] <input>` | Executa um role do núcleo nesta conversa. |
| `agent-pm-delegate run --list` | Lista todos os roles e tasks. |

## Início rápido

1. [Instale](#instalação) o plugin.
2. Numa nova session do Claude Code, verifique: `/agent-pm:act-as dummy-tester:echo hello` devolve `hello`.
3. Execute um role: `/agent-pm:act-as researcher <question>`. Sem configuração local, o relatório é salvo em `~/.agent-pm/adhoc/<date>-researcher-<name>/agent/prompt.md`; para publicá-lo, defina o destination do role ([Personalizar roles e tasks](../customizing-roles-and-tasks.md#change-a-roles-run-settings)).
4. Para executar roles a partir do Linear: [Quadro do Linear](../board.md).

## Personalizar roles e tasks

- Configurações de run por role (nível do modelo, effort, client, destination, comandos): `[delegate.roles.<role>]` em `~/.agent-pm/config.local.toml`.
- Os seus próprios roles, tasks e templates: um diretório de team listado em `[delegate] team_dirs`. Ele só adiciona; nunca altera um role embutido.
- Verificar: `agent-pm-delegate run --list`, depois `agent-pm-delegate run --role <role> --input <text> --dry-run`.

Formatos, regras e os limites do quadro: [Personalizar roles e tasks](../customizing-roles-and-tasks.md).

## Documentação

Estas páginas estão em inglês.

| Página | Cobre |
|---|---|
| [Customizing roles and tasks](../customizing-roles-and-tasks.md) | configurações de run, diretórios de team, arquivos de role e task, verificações |
| [Linear board](../board.md) | configuração, estados de issue e labels de task, operação do router, labels Agent (client por issue) |
| [Sessions and client policy](../sessions.md) | configurações de session, `agent-pm-session`, flags de client dos role runs |
| [State root](../state-root.md) | estrutura de `~/.agent-pm/`, o que limpa cada entrada, o que você pode apagar |
| [Upgrading](../upgrading.md) | passos de atualização, migrar da v1, checklist de smoke test |
| [Core architecture](../architecture.md) | camadas e pacotes (desenvolvedores) |
| [Tests](../tests.md) | lint, suítes de testes, cobertura (desenvolvedores) |
