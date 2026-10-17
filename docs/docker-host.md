# Docker Host

The Docker Host runs each Claude worker in its own container on Docker Desktop for Mac. The root-owned `agent-pm-broker` makes the container. Commands that work on a tmux worker (`send`, `read`, `attach`, `status`, `report`, `next-event`, `pending`, `close`) work the same way here. For the broker's messages, see [Docker broker protocol](docker-broker-protocol.md).

## Install

`agent-pm-broker`, the worker image and `with-secrets` come from the broker's own build step (TASK-559), which is not released yet. Until then, this section is a placeholder. That step will give the install and upgrade commands and the one-time `login claude`. The Docker Host calls the broker at `/usr/local/libexec/agent-pm/agent-pm-broker` and needs broker protocol `1`. With no broker, or a different protocol, a start fails before anything is built, and its message points here.

## Local config

Choose the Host and the directories a worker may work in, in your local config:

```toml
[session.hosts]
default = "docker"

[session.hosts.docker]
mount_roots = ["~/src"]
```

| Key | Default | Meaning |
|---|---|---|
| `docker` | `/usr/local/bin/docker` | the docker CLI, an absolute path |
| `mount_roots` | `[]` | directories a worker's cwd must lie under; empty: every start fails and asks you to set it |

Every docker call goes to `unix://<your account home>/.docker/run/docker.sock`, whatever `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` say.

## What is refused

A start fails before any container is made when:

- the cwd is outside every mount root;
- a mount root is your home or one of its parents;
- the cwd is your home, contains it, or overlaps a denied dir (`~/.agent-pm`, `~/.ssh`, `~/.config`, `~/.docker`, `~/Library`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.npmrc`, and each client's own dir);
- the client is not Claude;
- the run is a `restart` or `resume`, or a role run (its cwd is under `~/.agent-pm`);
- a container for that worker name still exists;
- the session is detached; only tui sessions run here.

These are not supported on the Docker Host yet.

## A Docker worker's cwd is untrusted

The cwd is mounted read-write. So the worker can write `.git/config` (for example `core.fsmonitor` or `core.hooksPath`) and `.git/hooks/*`. A plain `git status` or `git diff` that you run there on the host would then run a command the worker chose, as you. Treat the cwd as untrusted, and run host git there as:

```sh
git -c core.fsmonitor= -c core.hooksPath=/dev/null status
```

## File sharing and the network

- The cwd and `~/.agent-pm/docker-state` must be inside Docker Desktop's file sharing (Settings, Resources, File sharing; `/Users` by default). If they are not, the start fails with Docker's own error.
- Through Docker Desktop's `host.docker.internal`, a worker can reach any service on your host's loopback that has no authentication.

## Where its state lives

`~/.agent-pm/docker-state/<name>/` holds the client's config dir (mounted at the same path and remade at each start), the start manifests, `host.json` (what the host knows of the session), and the hook supervisor's `supervisor.lock`, `supervisor.pid` and `supervisor.log`. `close` keeps this dir.

## Recovering a leftover container

A container that outlived its worker (for example after a crash) blocks a new start of the same name. Find it, then remove it by hand:

```sh
docker --host "unix://$HOME/.docker/run/docker.sock" ps -a --filter label=agent-pm.session=<name>
docker --host "unix://$HOME/.docker/run/docker.sock" rm -f agent-pm-<name>-<generation>
```

After `docker rm -f`, `agent-pm-crew status` shows the worker as ended.
