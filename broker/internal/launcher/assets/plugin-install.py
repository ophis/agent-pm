"""Install image plugins with each client's native marketplace commands."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
COMMIT = re.compile(r"[a-f0-9]{40,64}")


def command(argv, env):
    return subprocess.run(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()


def installed(home, market, name):
    versions = list((home / "plugins/cache" / market / name).iterdir())
    if len(versions) != 1 or not versions[0].is_dir():
        raise ValueError(f"plugin-install: expected one installed version of {name}@{market}")
    root = versions[0]
    paths = [root / ".codex-plugin/plugin.json", root / ".claude-plugin/plugin.json"]
    manifest = json.loads(next(path for path in paths if path.is_file()).read_text())
    version = manifest.get("version")
    if (
        manifest.get("name") != name
        or not isinstance(version, str)
        or not version.isprintable()
        or not version
    ):
        raise ValueError(f"plugin-install: invalid installed manifest for {name}@{market}")
    return root, version


def install(plugins, root):
    claude, codex = root / "claude-plugins", root / "codex-plugins"
    shared = root / "plugins"
    for home in (claude, codex):
        (home / "plugins/cache").mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text("")
    shared.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(root),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "GIT_TERMINAL_PROMPT": "0",
    }
    markets = {}
    for plugin in plugins:
        name, repo = plugin["name"], plugin["repo"]
        if repo not in markets:
            # Discover only the marketplace just added, without depending on host or install records.
            before = set(
                (claude / "plugins/marketplaces").glob("*/.claude-plugin/marketplace.json")
            )
            command(["claude", "plugin", "marketplace", "add", repo], env)
            after = set((claude / "plugins/marketplaces").glob("*/.claude-plugin/marketplace.json"))
            added = after - before
            if len(added) != 1:
                raise ValueError(f"plugin-install: expected a new marketplace for {repo}")
            path = added.pop()
            market = json.loads(path.read_text())["name"]
            if not isinstance(market, str) or NAME.fullmatch(market) is None:
                raise ValueError("plugin-install: invalid marketplace name")
            command(["codex", "plugin", "marketplace", "add", repo], env)
            markets[repo] = market, path.parent.parent
        market, source = markets[repo]
        identifier = f"{name}@{market}"
        for client, verb, home in (("claude", "install", claude), ("codex", "add", codex)):
            command([client, "plugin", verb, identifier], env)
            location, version = installed(home, market, name)
            # Both clients retain their own checkout. Log each checkout's actual commit.
            checkout = source if client == "claude" else codex / ".tmp/marketplaces" / market
            commit = command(["git", "-C", str(checkout), "rev-parse", "HEAD"], env)
            if COMMIT.fullmatch(commit) is None:
                raise ValueError(f"plugin-install: cannot identify {client} marketplace commit")
            print(
                f"plugin-install: {client} {identifier} version={version} commit={commit}",
                flush=True,
            )
            if client == "claude":
                (shared / name).symlink_to(location)


def main(argv):
    if len(argv) != 2 or not Path(argv[1]).is_absolute():
        sys.exit("usage: plugin-install.py SOURCES ABSOLUTE_ROOT")
    install(json.loads(Path(argv[0]).read_text()), Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
