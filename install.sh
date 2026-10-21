#!/bin/bash
# Installs agent-pm on an Apple silicon Mac from nothing: dependencies, then your configuration
# (clients, Docker, OpenCode provider) into ~/.agent-pm/config.local.toml, then only what it needs.
# Asks sudo once, every login on the spot, and before writing your files. Rerun to finish or check.
#   curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh | bash -s -- [flags]
# AGENT_PM_INSTALL_ROOT prefixes its system paths; only tests set it.
set -euo pipefail

me=install.sh
root=${AGENT_PM_INSTALL_ROOT:-}
broker=$root/usr/local/libexec/agent-pm/agent-pm-broker
state_dir=$HOME/.agent-pm/install
state_file=$state_dir/state
ledger=$state_dir/added
config=$HOME/.agent-pm/config.local.toml
opencode_prefix=$HOME/.agent-pm/opencode
ref=main
clients_flag=
clients=
docker_flag=
docker=
provider_flag=
url_flag=
model_flag=
context_flag=
provider=
url=
model=
context=
prev_provider=
prev_url=
prev_model=
prev_context=
key_stdin=
password_stdin=
sudo_mine=
keepalive=
feeder=
askpass_dir=
current=
answer=
pkg=
user_path=$PATH
reload=
name_pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
url_pattern='^https?://[A-Za-z0-9._~:/%+-]+/v1$'
model_pattern='^[A-Za-z0-9._:/-]{1,200}$'
context_pattern='^[1-9][0-9]{0,8}$'
synopsis='install.sh [--ref <ref>] [--clients <claude,codex,opencode>] [--docker | --no-docker] [--opencode-provider <name>] [--opencode-url <url>] [--opencode-model <model>] [--opencode-context <tokens>] [--opencode-key-stdin] [--password-stdin]'

say() { printf '%s\n' "$*" >&2; }
step() {
  current=$1
  say "==> $1"
}
skip() { say "    skip: $*"; }
die() {
  say "$me: $*"
  exit 1
}

usage() {
  cat <<EOF
usage: $synopsis

  curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh | bash -s -- [flags]

  --ref <ref>                 git branch or tag of ophis/agent-pm to install the plugin and broker from (default main)
  --clients <list>            comma-separated: claude (Claude Code), codex (Codex), opencode (OpenCode); claude or codex needed
  --docker                    run workers in Docker: Docker Desktop, the broker and its logins
  --no-docker                 run workers on the local tmux Host; no Docker
  --opencode-provider <name>  the OpenAI-compatible provider OpenCode runs on, e.g. a LAN model server
  --opencode-url <url>        its base URL, ending /v1
  --opencode-model <model>    its model
  --opencode-context <tokens> its context window in tokens (default 65536)
  --opencode-key-stdin        read the provider's API key from stdin (tmux Host only)
  --password-stdin            read the sudo password from stdin (first line, before the key)

With --password-stdin or --opencode-key-stdin, stdin cannot carry the script:
  security find-generic-password -s <item> -w | bash -c "\$(curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/install.sh)" install.sh --password-stdin

It installs the dependencies, asks what the flags leave open, writes ~/.agent-pm/config.local.toml,
then installs and logs in only what that needs. A rerun keeps the last answers unless a flag changes
them, and skips what is done.
EOF
}

usage_error() {
  say "install.sh: $1"
  say "install.sh: usage: $synopsis"
  exit 2
}

# The canonical form of a clients list (claude,codex,opencode order), or exit 1 when it is not one.
clients_of() {
  local item out=
  case $1 in '' | ,* | *, | *,,* | *[!a-z,]*) return 1 ;; esac
  for item in ${1//,/ }; do
    case $item in claude | codex | opencode) ;; *) return 1 ;; esac
  done
  for item in claude codex opencode; do
    case ",$1," in *",$item,"*) out=${out:+$out,}$item ;; esac
  done
  case ",$out," in *,claude,* | *,codex,*) ;; *) return 1 ;; esac
  printf '%s' "$out"
}

valid_name() { [[ $1 =~ $name_pattern ]] && [ "$1" != ollama ]; }
valid_url() { [[ $1 =~ $url_pattern ]]; }
valid_model() { [[ $1 =~ $model_pattern ]]; }
valid_context() { [[ $1 =~ $context_pattern ]]; }

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
      --clients)
        [ $# -ge 2 ] || usage_error "--clients needs a value"
        clients_flag=$(clients_of "$2") || usage_error "--clients: want a comma-separated list of claude, codex, opencode with claude or codex, not $2"
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
      --opencode-provider)
        [ $# -ge 2 ] && valid_name "$2" || usage_error "--opencode-provider: want 1-64 letters, digits, _ or -, not ollama"
        provider_flag=$2
        shift
        ;;
      --opencode-url)
        [ $# -ge 2 ] && valid_url "$2" || usage_error "--opencode-url: want http(s)://<host>[:<port>]/.../v1, no user info or quotes"
        url_flag=$2
        shift
        ;;
      --opencode-model)
        [ $# -ge 2 ] && valid_model "$2" || usage_error "--opencode-model: want 1-200 letters, digits, ., _, :, / or -"
        model_flag=$2
        shift
        ;;
      --opencode-context)
        [ $# -ge 2 ] && valid_context "$2" || usage_error "--opencode-context: want a token count, 1-999999999"
        context_flag=$2
        shift
        ;;
      --opencode-key-stdin) key_stdin=1 ;;
      --password-stdin) password_stdin=1 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if { [ -n "$password_stdin" ] || [ -n "$key_stdin" ]; } && [ -t 0 ]; then
    usage_error "--password-stdin and --opencode-key-stdin read a pipe, not a terminal"
  fi
}

opencode_flags() { [ -n "$provider_flag$url_flag$model_flag$context_flag$key_stdin" ]; }

# The flags' conflicts that flags and State already decide, before anything runs.
early_checks() {
  local said_docker
  said_docker=$docker_flag
  [ -n "$said_docker" ] || said_docker=$(state_get docker)
  if [ -n "$key_stdin" ] && [ "$said_docker" = yes ]; then
    usage_error "--opencode-key-stdin: the broker reads a provider key only from a terminal; log in at its prompt instead"
  fi
  if opencode_flags && [ -n "$clients_flag" ]; then
    case ",$clients_flag," in *,opencode,*) ;; *) usage_error "--opencode-*: opencode is not among the clients" ;; esac
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

# Exit 0 when $1 is a path strictly under $HOME, absolute, without `..` or a tab or newline.
under_home() {
  case $1 in
    "$HOME"/?*) ;;
    *) return 1 ;;
  esac
  case /$1/ in */../* | */./*) return 1 ;; esac
  case $1 in *$'\t'* | *$'\n'*) return 1 ;; esac
  return 0
}

# Exit 0 when the fields are a Ledger record the cleanup can act on.
ledger_valid() {
  local plain='^[A-Za-z0-9._@+][A-Za-z0-9._@+-]*$'
  case $1:$# in
    dir:2 | file:2 | profile-file:2) under_home "$2" ;;
    profile:3) under_home "$2" && case $3 in *$'\t'* | *$'\n'* | '') false ;; esac ;;
    homebrew:2) [ "$2" = "$root/opt/homebrew" ] ;;
    clt:1 | broker:1) true ;;
    config:2) case $2 in created | backup) true ;; *) false ;; esac ;;
    formula:2) case $2 in gh | tmux | git | python | go | node) true ;; *) false ;; esac ;;
    cask:2) case $2 in iterm2 | claude-code | codex | docker-desktop) true ;; *) false ;; esac ;;
    claude-marketplace:2 | codex-marketplace:2) case $2 in agent-pm | claude-autopilot) true ;; *) false ;; esac ;;
    claude-plugin:2 | codex-plugin:2) case $2 in agent-pm@agent-pm | autopilot@claude-autopilot) true ;; *) false ;; esac ;;
    login:2) case $2 in gh | claude | codex) true ;; *) false ;; esac ;;
    launchd:2) case $2 in router | promote) true ;; *) false ;; esac ;;
    keychain:3) [[ $2 =~ $plain ]] && [[ $3 =~ $plain ]] ;;
    opencode-auth:3) under_home "$2" && [[ $3 =~ $name_pattern ]] ;;
    *) false ;;
  esac
}

# Records what the installer is about to create, once, for scripts/install-cleanup.sh.
ledger_add() {
  local line
  line=$(
    IFS=$'\t'
    printf '%s' "$*"
  )
  if ! ledger_valid "$@"; then
    say "    not recorded for cleanup: $*"
    return 0
  fi
  mkdir -p "$state_dir"
  if [ -f "$ledger" ] && grep -qxF -- "$line" "$ledger"; then return 0; fi
  printf '%s\n' "$line" >>"$ledger"
}

recorded() { [ -f "$ledger" ] && grep -qxF -- "$(
  IFS=$'\t'
  printf '%s' "$*"
)" "$ledger"; }

# Records directory $1 when it does not exist yet.
record_dir() { [ -e "$1" ] || ledger_add dir "$1"; }

# Records a directory under ~/.agent-pm, unless ~/.agent-pm itself is the installer's.
record_agent_dir() { recorded dir "$HOME/.agent-pm" || record_dir "$1"; }

# Records keychain item $1 / $2 when it does not exist yet.
record_item() {
  security find-generic-password -s "$1" -a "$2" </dev/null >/dev/null 2>&1 || ledger_add keychain "$1" "$2"
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
  if [ -f "$state_file" ] && [ "$(state_get "$1")" = "$2" ]; then return 0; fi
  mkdir -p "$state_dir"
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

on_exit() {
  local rc=$?
  sudo_end
  if [ "$rc" -ne 0 ] && [ -n "$current" ]; then
    say "$me: $current failed; rerun the same command to continue"
  fi
  if [ -n "$reload" ]; then
    case $reload in "$HOME"/*) reload="~/${reload#"$HOME"/}" ;; esac
    say "$me: in this terminal, run source $reload first (or open a new one): its PATH lacks what install.sh added to $reload"
  fi
}

# Reads the sudo password once (stdin's first line, else the terminal) and checks it. A FIFO then
# hands it from memory to the askpass helper of every later sudo -A, Homebrew's included (it passes
# -A when SUDO_ASKPASS is set): macOS's per-terminal tickets miss some of those sudo calls.
sudo_once() {
  local password= tries=0
  step sudo
  sudo -k </dev/null >/dev/null 2>&1 || true
  sudo_mine=1
  if [ -n "$password_stdin" ]; then
    IFS= read -r password || true
    if ! printf '%s\n' "$password" | sudo -S -p '' -v; then
      unset password
      die "sudo rejected the password from stdin"
    fi
  else
    while :; do
      printf 'Password:' >&3
      IFS= read -rs password <&3 || die "no answer on the terminal"
      printf '\n' >&3
      if printf '%s\n' "$password" | sudo -S -p '' -v 2>/dev/null; then break; fi
      tries=$((tries + 1))
      [ "$tries" -lt 3 ] || die "sudo refused"
      printf 'Sorry, try again.\n' >&3
    done
  fi
  askpass_dir=$(mktemp -d "${TMPDIR:-/tmp}/agent-pm-sudo.XXXXXX")
  case $askpass_dir in *"'"* | *$'\n'*) die "unusable TMPDIR: $askpass_dir" ;; esac
  mkfifo -m 600 "$askpass_dir/fifo"
  cat >"$askpass_dir/askpass" <<EOF
#!/bin/sh
IFS= read -r p <'$askpass_dir/fifo' && printf '%s\n' "\$p"
EOF
  chmod 700 "$askpass_dir/askpass"
  (
    trap '' PIPE
    while [ -p "$askpass_dir/fifo" ]; do
      printf '%s\n' "$password" 2>/dev/null >"$askpass_dir/fifo" || true
    done
  ) </dev/null >/dev/null 2>&1 &
  feeder=$!
  unset password
  export SUDO_ASKPASS=$askpass_dir/askpass
  (
    trap 'kill "$nap" 2>/dev/null; exit 0' TERM
    while kill -0 $$ 2>/dev/null; do
      sudo -n -v || true
      sleep 30 &
      nap=$!
      wait "$nap" || true
    done
    kill "$feeder" 2>/dev/null
    sleep 1
    rm -rf "$askpass_dir"
  ) </dev/null >/dev/null 2>&1 &
  keepalive=$!
}

# Stops the keep-alive and the askpass feeder, and drops the ticket sudo_once made.
sudo_end() {
  local pid
  for pid in $keepalive $feeder; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  if [ -n "$askpass_dir" ]; then rm -rf "$askpass_dir"; fi
  if [ -n "$sudo_mine" ]; then sudo -k </dev/null >/dev/null 2>&1 || true; fi
}

# Appends $1, which puts directory $2 on PATH, to the login shell's profile, with consent, unless
# it is there; then, when $2 is not on the PATH install.sh started with, on_exit says to source it.
profile_line() {
  local file=$HOME/.bash_profile
  case ${SHELL:-} in *zsh) file=$HOME/.zprofile ;; esac
  step "shell profile: $1"
  if [ -f "$file" ] && grep -qxF -- "$1" "$file"; then
    skip "in $file"
  elif consent "$file" "$1"; then
    if [ ! -e "$file" ]; then ledger_add profile-file "$file"; fi
    ledger_add profile "$file" "$1"
    if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then printf '\n' >>"$file"; fi
    printf '%s\n' "$1" >>"$file"
  else
    skip declined
    return
  fi
  case ":$user_path:" in *":$2:"*) ;; *) reload=$file ;; esac
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
    ledger_add homebrew "$prefix"
    if ! xcode-select -p </dev/null >/dev/null 2>&1; then ledger_add clt; fi
    installer=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)
    NONINTERACTIVE=1 bash -c "$installer" </dev/null
    brew=$prefix/bin/brew
  fi
  eval "$("$brew" shellenv)"
  hash -r
  profile_line "eval \"\$($brew shellenv)\"" "${brew%/*}"
}

brew_install() {
  if [ "$1" = --cask ]; then ledger_add cask "$2"; else ledger_add formula "$1"; fi
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
  command -v python3 >/dev/null && python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' </dev/null 2>/dev/null
}

has() { case ",$clients," in *",$1,"*) return 0 ;; esac; return 1; }

dependencies() {
  step dependencies
  formula gh
  formula tmux
  formula git
  if python_ok; then skip "python3 is 3.11+"; else brew_install python; fi
  cask iterm2 "" "$root/Applications/iTerm.app"
}

# What an existing config.local.toml says: "<[session.hosts] default>\t<[delegate] client>".
config_says() {
  [ -f "$config" ] || return 0
  python3 -I -c '
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
except (OSError, tomllib.TOMLDecodeError):
    sys.exit()
def get(*keys):
    node = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else ""
print(get("session", "hosts", "default") + "\t" + get("delegate", "client"))' "$config"
}

# ask_valid <question> <check>: asks until <check> accepts the answer.
ask_valid() {
  while :; do
    ask "$1"
    if "$2" "$answer"; then return 0; fi
  done
}

# The configuration: each answer from its flag, else the last answer (State), else the config, else a question.
answers() {
  local said default client offer=claude,codex
  step configuration
  said=$(config_says)
  default=${said%%$'\t'*}
  client=${said#*$'\t'}
  if [ -n "$clients_flag" ]; then
    clients=$clients_flag
  elif clients=$(state_get clients) && [ -n "$clients" ]; then
    :
  elif [ "$client" = codex ]; then
    clients=codex
  else
    if opencode_flags; then offer=claude,codex,opencode; fi
    while :; do
      ask "Which clients, comma-separated (claude, codex, opencode)? [$offer]"
      if clients=$(clients_of "${answer:-$offer}"); then break; fi
    done
  fi
  if [ -n "$docker_flag" ]; then
    docker=$docker_flag
  elif docker=$(state_get docker) && [ -n "$docker" ]; then
    :
  elif [ -n "$default" ]; then
    if [ "$default" = docker ]; then docker=yes; else docker=no; fi
  elif ask_yn "Run workers in Docker (Docker Desktop and the agent-pm broker)?"; then
    docker=yes
  else
    docker=no
  fi
  if opencode_flags && ! has opencode; then usage_error "--opencode-*: opencode is not among the clients"; fi
  if [ -n "$key_stdin" ] && [ "$docker" = yes ]; then
    usage_error "--opencode-key-stdin: the broker reads a provider key only from a terminal; log in at its prompt instead"
  fi
  prev_provider=$(state_get opencode_provider)
  prev_url=$(state_get opencode_url)
  prev_model=$(state_get opencode_model)
  prev_context=$(state_get opencode_context)
  if has opencode; then
    provider=${provider_flag:-$prev_provider}
    url=${url_flag:-$prev_url}
    model=${model_flag:-$prev_model}
    context=${context_flag:-$prev_context}
    if [ -z "$provider" ]; then
      ask_valid "OpenCode provider name:" valid_name
      provider=$answer
    fi
    if [ -z "$url" ]; then
      ask_valid "Its base URL (ending /v1):" valid_url
      url=$answer
    fi
    if [ -z "$model" ]; then
      ask_valid "Its model:" valid_model
      model=$answer
    fi
    while [ -z "$context" ]; do
      ask "Its context window in tokens [65536]:"
      if valid_context "${answer:-65536}"; then context=${answer:-65536}; fi
    done
    state_set opencode_provider "$provider"
    state_set opencode_url "$url"
    state_set opencode_model "$model"
    state_set opencode_context "$context"
  fi
  state_set clients "$clients"
  state_set docker "$docker"
  say "    clients: $clients, Docker: $docker${provider:+, OpenCode provider: $provider}"
}

# Plans the edits that give config.local.toml (or a new one) the wanted settings; writes the result to
# $1 and prints the changes (exit 3), prints only them when line edits cannot apply them (exit 4), or exit 0.
config_plan() {
  python3 -I -c '
import os, re, sys, tomllib
path, out, default, harness, client, exe, name, url, model, context, prev_name, prev_url, prev_model, prev_context = sys.argv[1:]
fresh = not os.path.exists(path)
text = ""
if not fresh:
    with open(path, encoding="utf-8") as f:
        text = f.read()
try:
    data = tomllib.loads(text)
except tomllib.TOMLDecodeError as error:
    sys.exit(f"{path}: {error}")
def get(data, table, key=None):
    node = data
    for part in table.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if key is None:
        return node
    return node.get(key) if isinstance(node, dict) else None
wanted = []
def want(table, key, value):
    if get(data, table, key) != value:
        wanted.append((table, key, value))
if fresh or (get(data, "session.hosts", "default") or "tmux") != default:
    wanted.append(("session.hosts", "default", default))
if client and (get(data, "delegate", "client") or "claude") != client:
    wanted.append(("delegate", "client", client))
if harness and get(data, "delegate.delegates", "harness_host") in (None, "", "docker"):
    wanted.append(("delegate.delegates", "harness_host", harness))
if name:
    target, before = f"{name}/{model}", f"{prev_name}/{prev_model}" if prev_name else None
    if get(data, "session.clients.opencode", "executable") in (None, ""):
        want("session.clients.opencode", "executable", exe)
    def ours(value):
        return value is None or (isinstance(value, str) and (value.startswith("ollama/") or value == before))
    if ours(get(data, "session.clients.opencode", "model")):
        want("session.clients.opencode", "model", target)
    table = f"session.clients.opencode.providers.{name}"
    found = get(data, table)
    before_context = int(prev_context) if prev_context else None
    if found is None or (name == prev_name and found.get("base_url") == prev_url and found.get("models") == [prev_model]
                         and found.get("context") in (None, before_context)):
        want(table, "base_url", url)
        want(table, "models", [model])
        want(table, "context", int(context))
    for tier in "1234":
        if ours(get(data, "session.clients.opencode.tiers", tier)):
            want("session.clients.opencode.tiers", tier, target)
if not wanted:
    sys.exit(0)
def toml(value):
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return str(value) if isinstance(value, int) else f"\"{value}\""
lines = text.splitlines(keepends=True)
if fresh:
    lines = ["# Written by install.sh. Keys and defaults: each layer\x27s packaged config.toml (its `# local:` lines);\n",
             "# the Linear board: docs/board.md#setup.\n"]
if lines and not lines[-1].endswith("\n"):
    lines[-1] += "\n"
for table, key, value in wanted:
    new = f"{key} = {toml(value)}\n"
    print(f"[{table}] {new.strip()}")
    starts = [i for i, line in enumerate(lines) if line.strip() == f"[{table}]"]
    if not starts:
        lines += (["\n"] if lines and not (fresh and len(lines) == 2) else []) + [f"[{table}]\n", new]
        continue
    end = starts[0] + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    hits = [i for i in range(starts[0] + 1, end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])]
    if hits:
        lines[hits[0]] = new
    else:
        lines.insert(end if fresh else starts[0] + 1, new)
result = "".join(lines)
try:
    edited = tomllib.loads(result)
except tomllib.TOMLDecodeError:
    sys.exit(4)
if any(get(edited, table, key) != value for table, key, value in wanted):
    sys.exit(4)
with open(out, "w", encoding="utf-8") as f:
    f.write(result)
sys.exit(3)' "$config" "$1" "$wanted_default" "$wanted_harness" "$wanted_client" \
    "$opencode_prefix/bin/opencode" "$provider" "$url" "$model" "$context" \
    "$prev_provider" "$prev_url" "$prev_model" "$prev_context"
}

config_file() {
  local wanted_default=tmux wanted_harness= wanted_client= changes rc=0 plan
  step config.local.toml
  if [ "$docker" = yes ]; then
    wanted_default=docker
    wanted_harness=tmux
  fi
  if ! has claude; then wanted_client=codex; fi
  mkdir -p "$state_dir"
  plan=$state_dir/config.local.toml.plan
  changes=$(config_plan "$plan") || rc=$?
  case $rc in
    0) skip "has the configuration" ;;
    3)
      if [ ! -f "$config" ]; then changes=$(cat "$plan"); fi
      if consent "$config" "$changes"; then
        if [ -f "$config" ]; then backup_config; else ledger_add config created; fi
        mv "$plan" "$config"
      else
        rm -f "$plan"
        skip declined
      fi
      ;;
    4)
      say "    set these by hand in $config:"
      say "$changes"
      skip "set it by hand"
      ;;
    *) die "cannot read $config" ;;
  esac
}

# Keeps the user's own config.local.toml once, before the installer's first edit (the cleanup restores it).
backup_config() {
  local backup=$state_dir/config.local.toml.orig
  if recorded config created || recorded config backup; then return 0; fi
  cp "$config" "$backup.tmp"
  mv "$backup.tmp" "$backup"
  ledger_add config backup
}

apps() {
  step apps
  record_dir "$HOME/.config/gh"
  if has claude; then
    record_dir "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    if [ -z "${CLAUDE_CONFIG_DIR:-}" ] && [ ! -e "$HOME/.claude.json" ]; then ledger_add file "$HOME/.claude.json"; fi
  fi
  if has codex; then record_dir "${CODEX_HOME:-$HOME/.codex}"; fi
  if [ "$docker" = yes ]; then
    record_dir "$HOME/.docker"
    record_dir "$HOME/Library/Containers/com.docker.docker"
    record_dir "$HOME/Library/Group Containers/group.com.docker"
  fi
  if has claude; then cask claude-code claude; else skip "Claude Code: not chosen"; fi
  if has codex; then cask codex codex; else skip "Codex: not chosen"; fi
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
    ledger_add login gh
    gh auth login --hostname github.com --git-protocol https --web <&3 >&3 2>&3
  fi
  step "Claude Code login"
  if ! has claude; then
    skip "Claude Code not chosen"
  elif claude auth status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    ledger_add login claude
    claude auth login <&3 >&3 2>&3
  fi
  step "Codex login"
  if ! has codex; then
    skip "Codex not chosen"
  elif codex login status </dev/null >/dev/null 2>&1; then
    skip "logged in"
  else
    ledger_add login codex
    codex login <&3 >&3 2>&3
  fi
}

# Exit 0: marketplace $1 is $2 at ref $3 ("*": any ref); 1: absent; 2: another source or ref.
claude_market() {
  claude plugin marketplace list --json </dev/null | python3 -I -c '
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
  claude plugin list --json </dev/null | python3 -I -c '
import json, sys
found = [p for p in json.load(sys.stdin) if p.get("id") == sys.argv[1]]
sys.exit(1 if not found else 0 if found[0].get("enabled") else 2)' "$1"
}

codex_market() {
  python3 -I -c '
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

# Exit 0: plugin $1 installed and enabled; 1: absent; 2: disabled.
codex_plugin() {
  codex plugin list --json </dev/null | python3 -I -c '
import json, sys
found = [p for p in json.load(sys.stdin).get("installed", []) if p.get("pluginId") == sys.argv[1]]
sys.exit(1 if not found else 0 if found[0].get("enabled") else 2)' "$1"
}

# A re-pointed marketplace (rc 2) existed before, and so may its plugin: neither is recorded.
claude_plugins() {
  local source=ophis/agent-pm rc id repointed=
  [ "$ref" = main ] || source="ophis/agent-pm#$ref"
  step "Claude Code plugins"
  if ! has claude; then
    skip "Claude Code not chosen"
    return
  fi
  rc=0
  claude_market agent-pm ophis/agent-pm "$ref" || rc=$?
  if [ "$rc" = 0 ]; then
    skip "marketplace agent-pm at $ref"
  else
    if [ "$rc" = 2 ]; then
      say "    marketplace agent-pm has another source: re-adding it from $source"
      repointed=1
      claude plugin uninstall agent-pm@agent-pm </dev/null >&2 || true
      claude plugin marketplace remove agent-pm </dev/null >&2
    fi
    if [ "$rc" = 1 ]; then ledger_add claude-marketplace agent-pm; fi
    claude plugin marketplace add "$source" </dev/null >&2
  fi
  rc=0
  claude_market claude-autopilot ophis/claude-autopilot '*' || rc=$?
  if [ "$rc" = 1 ]; then
    ledger_add claude-marketplace claude-autopilot
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
      *)
        if [ -z "$repointed" ] || [ "$id" != agent-pm@agent-pm ]; then ledger_add claude-plugin "$id"; fi
        claude plugin install "$id" </dev/null >&2
        ;;
    esac
  done
}

codex_plugins() {
  local rc id repointed=
  step "Codex plugins"
  if ! has codex; then
    skip "Codex not chosen"
    return
  fi
  rc=0
  codex_market agent-pm https://github.com/ophis/agent-pm.git "$ref" || rc=$?
  if [ "$rc" = 0 ]; then
    skip "marketplace agent-pm at $ref"
  else
    if [ "$rc" = 2 ]; then
      say "    marketplace agent-pm has another source: re-adding it at $ref"
      repointed=1
      codex plugin remove agent-pm@agent-pm </dev/null >&2 || true
      codex plugin marketplace remove agent-pm </dev/null >&2
    fi
    if [ "$rc" = 1 ]; then ledger_add codex-marketplace agent-pm; fi
    if [ "$ref" = main ]; then
      codex plugin marketplace add ophis/agent-pm </dev/null >&2
    else
      codex plugin marketplace add ophis/agent-pm --ref "$ref" </dev/null >&2
    fi
  fi
  rc=0
  codex_market claude-autopilot https://github.com/ophis/claude-autopilot.git '*' || rc=$?
  if [ "$rc" = 1 ]; then
    ledger_add codex-marketplace claude-autopilot
    codex plugin marketplace add ophis/claude-autopilot </dev/null >&2
  else
    skip "marketplace claude-autopilot"
  fi
  for id in agent-pm@agent-pm autopilot@claude-autopilot; do
    rc=0
    codex_plugin "$id" || rc=$?
    case $rc in
      0) skip "$id installed" ;;
      2) codex plugin add "$id" </dev/null >&2 ;;
      *)
        if [ -z "$repointed" ] || [ "$id" != agent-pm@agent-pm ]; then ledger_add codex-plugin "$id"; fi
        codex plugin add "$id" </dev/null >&2
        ;;
    esac
  done
}

# The installed plugin: the whole repo at $ref, source of the broker, ctmux and jobs. Claude Code's
# install first, else Codex's newest, as manager/skills/manage/scripts/launcher looks.
plugin_dir() {
  step "plugin directory"
  if has claude; then
    pkg=$(claude plugin list --json </dev/null | python3 -I -c '
import json, sys
print(next((p.get("installPath", "") for p in json.load(sys.stdin) if p.get("id") == "agent-pm@agent-pm"), ""))')
  else
    pkg=$(python3 -I -c '
import glob, os, sys
def version(path):
    return [int(part) if part.isdigit() else -1 for part in os.path.basename(path).split(".")]
found = sorted(glob.glob(os.path.join(glob.escape(sys.argv[1]), "*")), key=version, reverse=True)
print(found[0] if found else "")' "${CODEX_HOME:-$HOME/.codex}/plugins/cache/agent-pm/agent-pm")
  fi
  [ -n "$pkg" ] && [ -d "$pkg" ] || die "no installed agent-pm plugin directory"
  say "    $pkg"
}

# go_const <file> <name>: the string value of `const <name> = "..."` in a Go file of the plugin.
go_const() {
  local value
  value=$(sed -n "s/^const $2 = \"\\([0-9.]*\\)\"\$/\\1/p" "$pkg/$1")
  [ -n "$value" ] || die "no $2 in $pkg/$1"
  printf '%s' "$value"
}

opencode_install() {
  local version found=
  step "OpenCode install"
  if ! has opencode; then
    skip "OpenCode not chosen"
    return
  fi
  version=$(go_const broker/internal/launcher/versions.go opencodeMinVersion)
  formula node
  record_agent_dir "$opencode_prefix"
  record_dir "${XDG_DATA_HOME:-$HOME/.local/share}/opencode"
  record_dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
  record_dir "${XDG_STATE_HOME:-$HOME/.local/state}/opencode"
  record_dir "${XDG_CACHE_HOME:-$HOME/.cache}/opencode"
  if [ -x "$opencode_prefix/bin/opencode" ]; then
    found=$("$opencode_prefix/bin/opencode" --version </dev/null 2>/dev/null || true)
  fi
  if [ "$found" = "$version" ]; then
    skip "OpenCode $version in $opencode_prefix"
  else
    npm install -g --prefix "$opencode_prefix" --cache "$opencode_prefix/.npm-cache" "opencode-ai@$version" </dev/null >&2
    # npm's allowScripts skips opencode-ai's postinstall, which puts the platform binary in place of a stub.
    node "$opencode_prefix/lib/node_modules/opencode-ai/postinstall.mjs" </dev/null >&2
    found=$("$opencode_prefix/bin/opencode" --version </dev/null 2>/dev/null || true)
    [ "$found" = "$version" ] || die "OpenCode $version in $opencode_prefix does not run: opencode --version gave '$found'"
  fi
  case $(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "$url") in
    localhost | 127.0.0.1) ;;
    *) case $url in http://*) say "$url: http:// sends the API key in plaintext over the network (docs/docker-host.md › Risks)" ;; esac ;;
  esac
}

opencode_auth_file() { printf '%s' "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json"; }

# Exit 0 when OpenCode's auth.json has an entry for $1; its key is never read out.
opencode_has_key() {
  python3 -I -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        sys.exit(0 if sys.argv[2] in json.load(f) else 1)
except (OSError, ValueError):
    sys.exit(1)' "$(opencode_auth_file)" "$1"
}

# Adds {"<name>": {"type": "api", "key": <stdin line>}} to auth.json: 0600, atomic, others kept.
opencode_store_key() {
  python3 -I -c '
import json, os, sys, tempfile
path, name = sys.argv[1:]
key = sys.stdin.readline().rstrip("\n")
if not key:
    sys.exit("install.sh: no OpenCode key on stdin")
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except ValueError:
    sys.exit(f"install.sh: {path} is not JSON; nothing written")
data[name] = {"type": "api", "key": key}
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".auth.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
except BaseException:
    os.unlink(temp)
    raise' "$(opencode_auth_file)" "$provider"
}

# The provider's key: on Docker, the broker's login (after broker_install), once per broker binary
# while its keychain item is there; on tmux, OpenCode's auth.json.
opencode_key() {
  local sha
  step "OpenCode key"
  if ! has opencode; then
    skip "OpenCode not chosen"
  elif [ "$docker" = yes ]; then
    sha=$(shasum -a 256 "$broker" | cut -d' ' -f1)
    if [ "$(state_get login_opencode)" = "$sha $provider" ] \
      && security find-generic-password -s agent-pm-opencode -a "$provider" </dev/null >/dev/null 2>&1; then
      skip "$provider logged in to this broker"
    else
      record_item agent-pm-opencode "$provider"
      "$broker" login opencode "$provider" <&3 >&3 2>&3
      state_set login_opencode "$sha $provider"
    fi
  elif opencode_has_key "$provider"; then
    skip "$provider in $(opencode_auth_file)"
  else
    ledger_add opencode-auth "$(opencode_auth_file)" "$provider"
    if [ -n "$key_stdin" ]; then
      opencode_store_key
    else
      say "In opencode auth login pick Other, enter $provider, then paste its API key"
      "$opencode_prefix/bin/opencode" auth login <&3 >&3 2>&3
      opencode_has_key "$provider" || die "no $provider key in $(opencode_auth_file)"
    fi
  fi
}

ctmux_install() {
  local name missing=
  step "ctmux --install"
  for name in ctmux agent-pm-crew agent-pm-session agent-pm-delegate agent-pm-orchestrator; do
    [ -x "$HOME/.agent-pm/bin/$name" ] || missing=1
  done
  if [ -n "$missing" ]; then
    if [ -d "$HOME/.agent-pm/bin" ]; then
      for name in ctmux agent-pm-crew agent-pm-session agent-pm-delegate agent-pm-orchestrator; do
        recorded dir "$HOME/.agent-pm" || [ -e "$HOME/.agent-pm/bin/$name" ] || ledger_add file "$HOME/.agent-pm/bin/$name"
      done
    else
      record_agent_dir "$HOME/.agent-pm/bin"
    fi
    "$pkg/manager/skills/manage/scripts/ctmux" --install </dev/null >&2
  else
    skip "launchers in ~/.agent-pm/bin"
  fi
  profile_line 'export PATH="$HOME/.agent-pm/bin:$PATH"' "$HOME/.agent-pm/bin"
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
  [ -e "$root/usr/local/libexec/agent-pm" ] || ledger_add broker
  record_agent_dir "$HOME/.agent-pm/docker"
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
  ledger_add keychain agent-pm-bot "$id"
  key=$(base64 -i "$pem") && [ -n "$key" ] || die "cannot read $pem"
  printf 'add-generic-password -s agent-pm-bot -a %s -U -w %s %s\n' "$id" "$key" \
    "$HOME/Library/Keychains/login.keychain-db" | security -i >/dev/null 2>&1 || true
  unset key
  security find-generic-password -s agent-pm-bot </dev/null >/dev/null 2>&1 || die "agent-pm-bot key not stored"
}

# install, update, login, ready or busy: what the broker's pinned Codex needs.
codex_need() {
  "$broker" status </dev/null | python3 -I -c '
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
  if ! has claude; then
    skip "Claude Code not chosen"
  elif [ "$(state_get login_claude)" = "$sha" ]; then
    skip "done for this broker"
  else
    say "Create a token with claude setup-token, then paste it at the broker's prompt"
    record_item agent-pm-claude default
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
    record_item agent-pm-github-users agent-pm-bot
    "$broker" login github agent-pm-bot <&3 >&3 2>&3
    state_set login_github "$sha"
  fi
  step "broker Codex install"
  if ! has codex; then
    skip "Codex not chosen"
    step "broker login: Codex"
    skip "Codex not chosen"
  else
    min=$(go_const broker/internal/codex/versions.go MinVersion)
    need=$(codex_need "$min")
    case $need in
      busy) die "broker busy; rerun" ;;
      install | update) sudo -A "$broker" "$need-codex" "$min" </dev/null >&2 ;;
      *) skip "Codex $min or newer installed" ;;
    esac
    step "broker login: Codex"
    if [ "$(codex_need "$min")" = ready ]; then
      skip "logged in"
    else
      record_item agent-pm-codex auth
      "$broker" login codex <&3 >&3 2>&3
    fi
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

# Exit 0 when config.local.toml has [orchestrator] tracker = "linear".
linear_board() {
  [ -f "$config" ] && python3 -I -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    sys.exit(0 if tomllib.load(f).get("orchestrator", {}).get("tracker") == "linear" else 1)' "$config"
}

# "<service>\t<account>" for each Linear key config.local.toml names.
linear_services() {
  python3 -I -c '
import re, sys, tomllib
with open(sys.argv[1], "rb") as f:
    linear = tomllib.load(f).get("orchestrator", {}).get("linear", {})
found = [(linear.get(k), linear.get(k)) for k in ("harness_key", "manager_key")]
found += [(role.get("key"), role.get("account") or role.get("key")) for role in linear.get("roles", {}).values()]
for service, account in found:
    if not service:
        continue
    if all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]*", v) for v in (service, account)):
        print(f"{service}\t{account}")
    else:
        print(f"install.sh: skipping Linear key {service!r}: not a plain keychain name", file=sys.stderr)' "$config"
}

linear_keys() {
  local list service account
  step "Linear keys"
  if ! linear_board; then
    skip 'no tracker = "linear" in ~/.agent-pm/config.local.toml (docs/board.md#setup)'
    return
  fi
  list=$(linear_services)
  while IFS=$'\t' read -r service account; do
    [ -n "$service" ] || continue
    if security find-generic-password -s "$service" </dev/null >/dev/null 2>&1; then
      skip "$service in the keychain"
    else
      ledger_add keychain "$service" "$account"
      say "Linear key $service: paste its API key at security's prompt (nothing echoes)"
      security add-generic-password -s "$service" -a "$account" -w <&3 >&3 2>&3
    fi
  done <<<"$list"
}

# Writes the board job $1's plist for this user to $2, from the plugin's: the launcher in ~/.agent-pm/bin.
board_plist() {
  python3 -I -c '
import sys
source, out, home = sys.argv[1:]
with open(source, encoding="utf-8") as f:
    text = f.read()
shipped = "/Users/francis/playground/agent-pm/bin/agent-pm-orchestrator"
if shipped not in text:
    sys.exit(f"install.sh: {source} does not run {shipped}")
text = text.replace(shipped, "\0").replace("/Users/francis/", home + "/")
text = text.replace("\0", home + "/.agent-pm/bin/agent-pm-orchestrator")
with open(out, "w", encoding="utf-8") as f:
    f.write(text)' "$pkg/orchestrator/launchd/com.ophis.agent-pm.$1.plist" "$2" "$HOME"
}

board_jobs() {
  local job target new offered= shown= uid
  step "board launchd jobs"
  if ! linear_board; then
    skip "no Linear board"
    return
  fi
  mkdir -p "$state_dir"
  for job in router promote; do
    target=$HOME/Library/LaunchAgents/com.ophis.agent-pm.$job.plist
    new=$state_dir/com.ophis.agent-pm.$job.plist
    board_plist "$job" "$new"
    if [ -f "$target" ] && cmp -s "$new" "$target"; then
      skip "$job installed"
    elif [ -e "$target" ]; then
      skip "$target exists; remove it to let install.sh install the job"
    else
      offered="$offered $job"
      shown="$shown$(printf '%s:\n%s' "$target" "$(cat "$new")")
"
    fi
  done
  [ -n "$offered" ] || return 0
  if ! consent "$HOME/Library/LaunchAgents" "$shown"; then
    skip declined
    return
  fi
  uid=$(id -u)
  record_agent_dir "$HOME/.agent-pm/logs"
  record_dir "$HOME/Library/LaunchAgents"
  mkdir -p "$HOME/.agent-pm/logs" "$HOME/Library/LaunchAgents"
  for job in $offered; do
    ledger_add launchd "$job"
    target=$HOME/Library/LaunchAgents/com.ophis.agent-pm.$job.plist
    cp "$state_dir/com.ophis.agent-pm.$job.plist" "$target"
    launchctl bootout "gui/$uid/com.ophis.agent-pm.$job" </dev/null >/dev/null 2>&1 || true
    launchctl enable "gui/$uid/com.ophis.agent-pm.$job" </dev/null
    launchctl bootstrap "gui/$uid" "$target" </dev/null
  done
}

# The terminal's own device (/dev/ttys<n>), else /dev/tty: macOS's kqueue refuses /dev/tty, so
# Bun-built clients (Claude Code, OpenCode) die on it with EINVAL.
terminal() {
  local name
  name=$(ps -o tty= -p $$ </dev/null 2>/dev/null) || name=
  name=${name//[[:space:]]/}
  case $name in
    ttys[0-9]*)
      if [ -c "$root/dev/$name" ] && (: <>"$root/dev/$name") 2>/dev/null; then
        printf '%s' "$root/dev/$name"
        return
      fi
      ;;
  esac
  printf '%s' "$root/dev/tty"
}

# The banner a finished install ends with, a line at a time on a terminal.
celebrate() {
  local line pause=
  if [ -t 2 ]; then pause=0.08; fi
  while IFS= read -r line; do
    say "$line"
    if [ -n "$pause" ]; then sleep "$pause"; fi
  done <<'EOF'

                        _
  __ _  __ _  ___ _ __ | |_      _ __  _ __ ___
 / _` |/ _` |/ _ \ '_ \| __|____| '_ \| '_ ` _ \
| (_| | (_| |  __/ | | | ||_____| |_) | | | | | |
 \__,_|\__, |\___|_| |_|\__|    | .__/|_| |_| |_|
       |___/                    |_|

EOF
  say "agent-pm is installed (ref $ref, clients $clients, Docker $docker)."
  say "Welcome aboard, and congrats: your crew is ready. Open a commander with ctmux <name>."
}

main() {
  local platform
  set +x
  parse_args "$@"
  early_checks
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] || die "needs macOS on Apple silicon (Darwin arm64), not $platform"
  if [ ! -e "$root/dev/tty" ] || ! (: <>"$root/dev/tty") 2>/dev/null; then die "needs a terminal for its questions and logins"; fi
  exec 3<>"$(terminal)"
  trap on_exit EXIT
  [ -d "$HOME/.agent-pm" ] || ledger_add dir "$HOME/.agent-pm"
  sudo_once
  homebrew
  dependencies
  answers
  config_file
  apps
  client_logins
  claude_plugins
  codex_plugins
  plugin_dir
  opencode_install
  docker_steps
  opencode_key
  ctmux_install
  linear_keys
  board_jobs
  current=
  celebrate
}

main "$@"
