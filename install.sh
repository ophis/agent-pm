#!/bin/bash
# Installs agent-pm on an Apple silicon Mac from nothing: Homebrew, tools, clients, plugins, logins.
# Asks sudo once, every login on the spot, and before writing your config. Rerun to finish or check.
#   curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh | bash -s -- [flags]
# AGENT_PM_INSTALL_ROOT prefixes its system paths; only tests set it.
set -euo pipefail

root=${AGENT_PM_INSTALL_ROOT:-}
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
  current=
  say "agent-pm is installed (ref $ref, Docker $docker)."
}

main "$@"
