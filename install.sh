#!/bin/bash
# Installs agent-pm on an Apple silicon Mac from nothing: Homebrew, tools, clients, plugins, logins.
# Asks sudo once, every login on the spot, and before writing your config. Rerun to finish or check.
#   curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh | bash -s -- [flags]
# AGENT_PM_INSTALL_ROOT prefixes its system paths; only tests set it.
set -euo pipefail

root=${AGENT_PM_INSTALL_ROOT:-}
broker=$root/usr/local/libexec/agent-pm/agent-pm-broker
state_file=$HOME/.agent-pm/install/state
config=$HOME/.agent-pm/config.local.toml
ref=main
docker_flag=
docker=
password_stdin=
sudo_mine=
keepalive=
current=
answer=

say() { printf '%s\n' "$*" >&2; }
step() {
  current=$1
  say "==> $1"
}
skip() { say "    skip: $*"; }
die() {
  say "install.sh: $*"
  exit 1
}

usage() {
  cat <<'EOF'
usage: install.sh [--ref <ref>] [--docker | --no-docker] [--password-stdin]

  curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh | bash -s -- [flags]

  --ref <ref>         git branch or tag of ophis/agent-pm to install the plugin and broker from (default main)
  --docker            run workers in Docker: Docker Desktop, the broker and its logins
  --no-docker         run workers on the local tmux Host; no Docker
  --password-stdin    read the sudo password from stdin; stdin then cannot carry the script:
    security find-generic-password -s <item> -w | bash -c "$(curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh)" install.sh --password-stdin

Without --docker or --no-docker it asks once and keeps the answer. A rerun skips what is done.
EOF
}

usage_error() {
  say "install.sh: $1"
  say "install.sh: usage: install.sh [--ref <ref>] [--docker | --no-docker] [--password-stdin]"
  exit 2
}

parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --ref)
        [ $# -ge 2 ] || usage_error "--ref needs a value"
        case $2 in
          '' | -* | *[!A-Za-z0-9._/-]*) usage_error "--ref: not a git ref: $2" ;;
        esac
        ref=$2
        shift
        ;;
      --docker)
        [ "$docker_flag" != no ] || usage_error "--docker and --no-docker exclude each other"
        docker_flag=yes
        ;;
      --no-docker)
        [ "$docker_flag" != yes ] || usage_error "--docker and --no-docker exclude each other"
        docker_flag=no
        ;;
      --password-stdin) password_stdin=1 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if [ -n "$password_stdin" ] && [ -t 0 ]; then
    usage_error "--password-stdin reads a pipe, not a terminal"
  fi
}

# A question on the terminal (fd 3); its answer lands in $answer.
ask() {
  printf '%s ' "$1" >&3
  IFS= read -r answer <&3 || die "no answer on the terminal"
}

ask_yn() {
  ask "$1 [y/N]"
  case $answer in
    [yY] | [yY][eE][sS]) return 0 ;;
  esac
  return 1
}

# Shows what would be written to $1 and asks before writing it.
consent() {
  printf 'install.sh will write to %s:\n%s\n' "$1" "$2" >&3
  ask_yn "Write it?"
}

state_get() {
  local key value
  [ -f "$state_file" ] || return 0
  while IFS='=' read -r key value; do
    if [ "$key" = "$1" ]; then printf '%s' "$value"; fi
  done <"$state_file"
}

state_set() {
  local key value
  mkdir -p "${state_file%/*}"
  {
    if [ -f "$state_file" ]; then
      while IFS='=' read -r key value; do
        if [ "$key" != "$1" ]; then printf '%s=%s\n' "$key" "$value"; fi
      done <"$state_file"
    fi
    printf '%s=%s\n' "$1" "$2"
  } >"$state_file.tmp"
  mv "$state_file.tmp" "$state_file"
}

# [session.hosts] default from config.local.toml, read as text: python may not exist yet.
config_default() {
  local line in_table= pattern='^default *= *"([A-Za-z0-9_-]+)"'
  [ -f "$config" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      '[session.hosts]')
        in_table=1
        continue
        ;;
      '['*) in_table= ;;
    esac
    if [ -n "$in_table" ] && [[ $line =~ $pattern ]]; then
      printf '%s' "${BASH_REMATCH[1]}"
      return 0
    fi
  done <"$config"
}

docker_answer() {
  local found
  if [ -n "$docker_flag" ]; then
    docker=$docker_flag
  elif found=$(config_default) && [ -n "$found" ]; then
    if [ "$found" = docker ]; then docker=yes; else docker=no; fi
  elif found=$(state_get docker) && [ -n "$found" ]; then
    docker=$found
  elif ask_yn "Run workers in Docker (Docker Desktop and the agent-pm broker)?"; then
    docker=yes
  else
    docker=no
  fi
  state_set docker "$docker"
  say "Docker: $docker"
}

on_exit() {
  local rc=$?
  if [ -n "$keepalive" ]; then
    kill "$keepalive" 2>/dev/null || true
    wait "$keepalive" 2>/dev/null || true
  fi
  if [ -n "$sudo_mine" ]; then sudo -k </dev/null >/dev/null 2>&1 || true; fi
  if [ "$rc" -ne 0 ] && [ -n "$current" ]; then
    say "install.sh: $current failed; rerun the same command to continue"
  fi
}

sudo_once() {
  local password
  step sudo
  if sudo -n true </dev/null >/dev/null 2>&1; then
    skip "already valid"
  elif [ -n "$password_stdin" ]; then
    IFS= read -r password || true
    if ! printf '%s\n' "$password" | sudo -S -p '' -v; then
      unset password
      die "sudo rejected the password from stdin"
    fi
    unset password
    sudo_mine=1
  else
    sudo -v <&3 >&3 2>&3 || die "sudo refused"
    sudo_mine=1
  fi
  (
    trap 'kill "$nap" 2>/dev/null; exit 0' TERM
    while kill -0 $$ 2>/dev/null; do
      sudo -n -v || true
      sleep 30 &
      nap=$!
      wait "$nap" || true
    done
  ) </dev/null >/dev/null 2>&1 &
  keepalive=$!
}

# Appends $1 to the login shell's profile, with consent, unless it is there.
profile_line() {
  local file=$HOME/.bash_profile
  case ${SHELL:-} in *zsh) file=$HOME/.zprofile ;; esac
  step "shell profile: $1"
  if [ -f "$file" ] && grep -qxF -- "$1" "$file"; then
    skip "in $file"
  elif consent "$file" "$1"; then
    printf '%s\n' "$1" >>"$file"
  else
    skip declined
  fi
}

homebrew() {
  local prefix=${HOMEBREW_PREFIX:-$root/opt/homebrew} brew installer
  step Homebrew
  if brew=$(command -v brew); then
    skip "installed"
  elif [ -x "$prefix/bin/brew" ]; then
    brew=$prefix/bin/brew
    skip "installed"
  else
    installer=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)
    NONINTERACTIVE=1 bash -c "$installer" </dev/null
    brew=$prefix/bin/brew
  fi
  eval "$("$brew" shellenv)"
  hash -r
  profile_line "eval \"\$($brew shellenv)\""
}

brew_install() {
  say "    brew install $*"
  brew install "$@" </dev/null
  hash -r
}

formula() {
  if brew list --formula "$1" </dev/null >/dev/null 2>&1; then
    skip "$1 installed"
  else
    brew_install "$1"
  fi
}

# cask <name> <command or app path that proves it installed>
cask() {
  if { [ -n "$2" ] && command -v "$2" >/dev/null; } || [ -e "${3:-/nonexistent}" ] \
    || brew list --cask "$1" </dev/null >/dev/null 2>&1; then
    skip "$1 installed"
  else
    brew_install --cask "$1"
  fi
}

python_ok() {
  command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' </dev/null 2>/dev/null
}

brew_packages() {
  step "brew packages"
  formula gh
  formula tmux
  formula git
  if python_ok; then skip "python3 is 3.11+"; else brew_install python; fi
  cask iterm2 "" "$root/Applications/iTerm.app"
  cask claude-code claude
  cask codex codex
  if [ "$docker" = yes ]; then
    cask docker-desktop "" "$root/Applications/Docker.app"
    if [ "$ref" = main ]; then skip "go: the broker comes from a release"; else formula go; fi
  else
    skip "Docker Desktop: Docker not chosen"
  fi
}

client_logins() {
  step "GitHub CLI login"
  if gh auth status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    gh auth login --hostname github.com --git-protocol https --web <&3 >&3 2>&3
  fi
  step "Claude Code login"
  if claude auth status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    claude auth login <&3 >&3 2>&3
  fi
  step "Codex login"
  if codex login status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    codex login <&3 >&3 2>&3
  fi
}

# Exit 0: marketplace $1 is $2 at ref $3 ("*": any ref); 1: absent; 2: another source or ref.
claude_market() {
  claude plugin marketplace list --json </dev/null | python3 -c '
import json, sys
name, repo, ref = sys.argv[1:]
refs = {"", "main"} if ref == "main" else {ref}
for m in json.load(sys.stdin):
    if m.get("name") == name:
        sys.exit(0 if m.get("repo") == repo and (ref == "*" or m.get("ref", "") in refs) else 2)
sys.exit(1)' "$1" "$2" "$3"
}

# Exit 0: plugin $1 installed and enabled; 1: absent; 2: disabled.
claude_plugin() {
  claude plugin list --json </dev/null | python3 -c '
import json, sys
found = [p for p in json.load(sys.stdin) if p.get("id") == sys.argv[1]]
sys.exit(1 if not found else 0 if found[0].get("enabled") else 2)' "$1"
}

codex_market() {
  python3 -c '
import sys, tomllib
path, name, source, ref = sys.argv[1:]
try:
    with open(path, "rb") as f:
        m = tomllib.load(f).get("marketplaces", {}).get(name)
except FileNotFoundError:
    m = None
refs = {"", "main"} if ref == "main" else {ref}
if m is None:
    sys.exit(1)
sys.exit(0 if m.get("source") == source and (ref == "*" or m.get("ref", "") in refs) else 2)' \
    "${CODEX_HOME:-$HOME/.codex}/config.toml" "$1" "$2" "$3"
}

codex_plugin_on() {
  codex plugin list --json </dev/null | python3 -c '
import json, sys
sys.exit(0 if any(p.get("pluginId") == sys.argv[1] and p.get("enabled") for p in json.load(sys.stdin).get("installed", [])) else 1)' "$1"
}

claude_plugins() {
  local source=ophis/agent-pm rc id
  [ "$ref" = main ] || source="ophis/agent-pm#$ref"
  step "Claude Code plugins"
  rc=0
  claude_market agent-pm ophis/agent-pm "$ref" || rc=$?
  if [ "$rc" = 0 ]; then
    skip "marketplace agent-pm at $ref"
  else
    if [ "$rc" = 2 ]; then
      say "    marketplace agent-pm has another source: re-adding it from $source"
      claude plugin uninstall agent-pm@agent-pm </dev/null >&2 || true
      claude plugin marketplace remove agent-pm </dev/null >&2
    fi
    claude plugin marketplace add "$source" </dev/null >&2
  fi
  rc=0
  claude_market claude-autopilot ophis/claude-autopilot '*' || rc=$?
  if [ "$rc" = 1 ]; then
    claude plugin marketplace add ophis/claude-autopilot </dev/null >&2
  else
    skip "marketplace claude-autopilot"
  fi
  for id in agent-pm@agent-pm autopilot@claude-autopilot; do
    rc=0
    claude_plugin "$id" || rc=$?
    case $rc in
      0) skip "$id installed" ;;
      2) claude plugin enable "$id" </dev/null >&2 ;;
      *) claude plugin install "$id" </dev/null >&2 ;;
    esac
  done
}

codex_plugins() {
  local rc id
  step "Codex plugins"
  rc=0
  codex_market agent-pm https://github.com/ophis/agent-pm.git "$ref" || rc=$?
  if [ "$rc" = 0 ]; then
    skip "marketplace agent-pm at $ref"
  else
    if [ "$rc" = 2 ]; then
      say "    marketplace agent-pm has another source: re-adding it at $ref"
      codex plugin remove agent-pm@agent-pm </dev/null >&2 || true
      codex plugin marketplace remove agent-pm </dev/null >&2
    fi
    if [ "$ref" = main ]; then
      codex plugin marketplace add ophis/agent-pm </dev/null >&2
    else
      codex plugin marketplace add ophis/agent-pm --ref "$ref" </dev/null >&2
    fi
  fi
  rc=0
  codex_market claude-autopilot https://github.com/ophis/claude-autopilot.git '*' || rc=$?
  if [ "$rc" = 1 ]; then
    codex plugin marketplace add ophis/claude-autopilot </dev/null >&2
  else
    skip "marketplace claude-autopilot"
  fi
  for id in agent-pm@agent-pm autopilot@claude-autopilot; do
    if codex_plugin_on "$id"; then skip "$id installed"; else codex plugin add "$id" </dev/null >&2; fi
  done
}

# The installed Claude Code plugin: the whole repo at $ref, source of the broker, ctmux and jobs.
plugin_dir() {
  step "plugin directory"
  pkg=$(claude plugin list --json </dev/null | python3 -c '
import json, sys
print(next((p.get("installPath", "") for p in json.load(sys.stdin) if p.get("id") == "agent-pm@agent-pm"), ""))')
  [ -n "$pkg" ] && [ -d "$pkg" ] || die "claude plugin list --json names no agent-pm@agent-pm directory"
  say "    $pkg"
}

ctmux_install() {
  local name missing=
  step "ctmux --install"
  for name in ctmux agent-pm-crew agent-pm-session agent-pm-delegate agent-pm-orchestrator; do
    [ -x "$HOME/.agent-pm/bin/$name" ] || missing=1
  done
  if [ -n "$missing" ]; then
    "$pkg/manager/skills/manage/scripts/ctmux" --install </dev/null >&2
  else
    skip "launchers in ~/.agent-pm/bin"
  fi
  profile_line 'export PATH="$HOME/.agent-pm/bin:$PATH"'
}

# sha256 of the plugin's broker sources: a change means the broker must be reinstalled.
broker_source() {
  (
    cd "$pkg"
    find broker -type f | LC_ALL=C sort | while IFS= read -r file; do shasum -a 256 "$file"; done
    shasum -a 256 .claude-plugin/plugin.json
  ) | shasum -a 256 | cut -d' ' -f1
}

docker_running() {
  step "Docker Desktop running"
  if docker info </dev/null >/dev/null 2>&1; then
    skip running
    return
  fi
  open -a Docker </dev/null
  while ! docker info </dev/null >/dev/null 2>&1; do
    ask "Finish Docker Desktop's first-run setup, then press Enter (or type skip)"
    [ "$answer" != skip ] || die "Docker Desktop not running"
  done
}

broker_install() {
  local source
  source=$(broker_source)
  if [ -x "$broker" ] && [ "$(state_get broker_source)" = "$source" ]; then
    step "Docker Desktop running"
    skip "broker current"
    step broker
    skip "installed from this plugin"
    return
  fi
  docker_running
  step broker
  if [ "$ref" = main ]; then
    sh "$pkg/broker/install.sh" </dev/null >&2
  else
    sh "$pkg/broker/install.sh" --build </dev/null >&2
  fi
  state_set broker_source "$source"
}

# Stores the agent-pm-bot App key from a .pem the user names; 1 when they skip it.
store_app_key() {
  local pem id key
  [ "$(state_get github_app_key)" != skipped ] || return 1
  ask "Path to the agent-pm-bot GitHub App private key (.pem), empty to skip:"
  if [ -z "$answer" ]; then
    state_set github_app_key skipped
    return 1
  fi
  pem=$answer
  [ -f "$pem" ] || die "no file $pem"
  while :; do
    ask "Its App ID (digits):"
    case $answer in '' | *[!0-9]*) ;; *) break ;; esac
  done
  id=$answer
  key=$(base64 -i "$pem")
  printf 'add-generic-password -s agent-pm-bot -a %s -U -w %s %s\n' "$id" "$key" \
    "$HOME/Library/Keychains/login.keychain-db" | security -i >/dev/null 2>&1 || true
  unset key
  security find-generic-password -s agent-pm-bot </dev/null >/dev/null 2>&1 || die "agent-pm-bot key not stored"
}

# install, update, login, ready or busy: what the broker's pinned Codex needs.
codex_need() {
  "$broker" status </dev/null | python3 -c '
import json, sys
codex = json.load(sys.stdin).get("codex") or {}
state = codex.get("state", "")
def version(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return (0,)
if state == "busy":
    print("busy")
elif state == "installation_invalid":
    print("install" if "install-codex" in codex.get("repair", "") else "update")
elif version(codex.get("version", "")) < version(sys.argv[1]):
    print("update")
else:
    print("ready" if state == "ready" else "login")' "$1"
}

broker_logins() {
  local sha min need
  sha=$(shasum -a 256 "$broker" | cut -d' ' -f1)
  step "broker login: Claude"
  if [ "$(state_get login_claude)" = "$sha" ]; then
    skip "done for this broker"
  else
    say "Create a token with claude setup-token, then paste it at the broker's prompt"
    claude setup-token <&3 >&3 2>&3
    "$broker" login claude <&3 >&3 2>&3
    state_set login_claude "$sha"
  fi
  step "broker login: GitHub"
  if [ "$(state_get login_github)" = "$sha" ]; then
    skip "done for this broker"
  elif ! security find-generic-password -s agent-pm-bot </dev/null >/dev/null 2>&1 && ! store_app_key; then
    skip "no agent-pm-bot App key (docs/docker-host.md#log-in)"
  else
    "$broker" login github agent-pm-bot <&3 >&3 2>&3
    state_set login_github "$sha"
  fi
  step "broker Codex install"
  min=$(sed -n 's/^const MinVersion = "\([0-9.]*\)"$/\1/p' "$pkg/broker/internal/codex/versions.go")
  [ -n "$min" ] || die "no MinVersion in $pkg/broker/internal/codex/versions.go"
  need=$(codex_need "$min")
  case $need in
    busy) die "broker busy; rerun" ;;
    install | update) sudo "$broker" "$need-codex" "$min" </dev/null >&2 ;;
    *) skip "Codex $min or newer installed" ;;
  esac
  step "broker login: Codex"
  if [ "$(codex_need "$min")" = ready ]; then
    skip "logged in"
  else
    "$broker" login codex <&3 >&3 2>&3
  fi
}

docker_steps() {
  local name
  if [ "$docker" != yes ]; then
    for name in "Docker Desktop running" broker "broker login: Claude" "broker login: GitHub" \
      "broker Codex install" "broker login: Codex"; do
      step "$name"
      skip "Docker not chosen"
    done
    return
  fi
  broker_install
  broker_logins
}

main() {
  local platform
  parse_args "$@"
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] || die "needs macOS on Apple silicon (Darwin arm64), not $platform"
  if [ ! -e "$root/dev/tty" ] || ! (: <>"$root/dev/tty") 2>/dev/null; then die "needs a terminal for its questions and logins"; fi
  exec 3<>"$root/dev/tty"
  trap on_exit EXIT
  docker_answer
  sudo_once
  homebrew
  brew_packages
  client_logins
  claude_plugins
  codex_plugins
  plugin_dir
  docker_steps
  ctmux_install
  current=
  say "agent-pm is installed (ref $ref, Docker $docker)."
}

main "$@"
