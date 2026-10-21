#!/bin/bash
# Internal, for test machines only (not user docs): removes what install.sh added, as recorded in
# ~/.agent-pm/install/added, and keeps what was there before. Lists it and asks first; --yes skips that.
#   curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/scripts/install-cleanup.sh | bash
# Its shared helpers are install.sh's, copied (install.sh stays one file for curl | bash);
# tests/integration/install_integration_test.py checks they match. AGENT_PM_INSTALL_ROOT: as install.sh's.
set -euo pipefail

me=install-cleanup.sh
root=${AGENT_PM_INSTALL_ROOT:-}
state_dir=$HOME/.agent-pm/install
ledger=$state_dir/added
config=$HOME/.agent-pm/config.local.toml
name_pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
yes=
password_stdin=
sudo_mine=
keepalive=
feeder=
askpass_dir=
current=
answer=

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

on_exit() {
  local rc=$?
  sudo_end
  if [ "$rc" -ne 0 ] && [ -n "$current" ]; then
    say "$me: $current failed; rerun the same command to continue"
  fi
}

usage() {
  cat <<EOF
usage: install-cleanup.sh [--yes] [--password-stdin]

Internal, for test machines only: removes what install.sh recorded in ~/.agent-pm/install/added,
so the machine is as before install.sh ran. Lists it and asks first; --yes skips the question
(automation only). Run by hand:
  curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/scripts/install-cleanup.sh | bash
With --password-stdin (sudo password on stdin), stdin cannot carry the script:
  security find-generic-password -s <item> -w | bash -c "\$(curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/<ref>/scripts/install-cleanup.sh)" install-cleanup.sh --yes --password-stdin
EOF
}

usage_error() {
  say "$me: $1"
  say "$me: usage: install-cleanup.sh [--yes] [--password-stdin]"
  exit 2
}

parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --yes) yes=1 ;;
      --password-stdin) password_stdin=1 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
    shift
  done
  if [ -n "$password_stdin" ] && [ -t 0 ]; then
    usage_error "--password-stdin reads a pipe, not a terminal"
  fi
}

# Removes record $1 (a whole Ledger line) once its action has succeeded.
drop() {
  grep -vxF -- "$1" "$ledger" >"$ledger.tmp" || true
  mv "$ledger.tmp" "$ledger"
}

# The Ledger's records of kind $1, one line each, in Ledger order.
of_kind() { grep -E "^$1(\$|"$'\t'")" "$ledger" || true; }

validate() {
  local line
  local -a fields
  while IFS= read -r line; do
    IFS=$'\t' read -r -a fields <<<"$line"
    ledger_valid "${fields[@]}" || die "not a record install.sh writes; nothing removed: $line"
  done <"$ledger"
}

show() {
  local kind
  printf 'install-cleanup.sh will remove what install.sh added:\n' >&3
  for kind in launchd keychain broker codex-plugin codex-marketplace claude-plugin claude-marketplace \
    login opencode-auth profile profile-file config cask formula homebrew clt file dir; do
    of_kind "$kind" | tr '\t' ' ' | sed 's/^/  /' >&3
  done
}

# need <command>: fails the Step when the tool a record needs is not on PATH; the record stays.
need() { command -v "$1" >/dev/null || die "$1 not found on PATH; its records stay for a rerun"; }

launchd_jobs() {
  local line job uid
  step "launchd jobs"
  uid=$(id -u)
  while IFS=$'\t' read -r _ job; do
    [ -n "$job" ] || continue
    line=launchd$'\t'$job
    launchctl bootout "gui/$uid/com.ophis.agent-pm.$job" </dev/null >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/com.ophis.agent-pm.$job.plist"
    drop "$line"
  done <<<"$(of_kind launchd)"
}

# Deletes keychain item -s $1 [-a $2]; exit 0 when it is gone or was never there.
delete_item() {
  local rc=0
  security delete-generic-password -s "$1" ${2:+-a "$2"} </dev/null >/dev/null 2>&1 || rc=$?
  [ "$rc" = 0 ] || [ "$rc" = 44 ] || die "security delete-generic-password -s $1 failed ($rc)"
  return "$rc"
}

keychain_items() {
  local service account
  step "keychain items"
  while IFS=$'\t' read -r _ service account; do
    [ -n "$service" ] || continue
    delete_item "$service" "$account" || true
    drop "keychain"$'\t'"$service"$'\t'"$account"
  done <<<"$(of_kind keychain)"
}

broker_files() {
  local service count
  step broker
  if ! grep -qx broker "$ledger"; then
    skip "not installed by install.sh"
    return
  fi
  sudo -A rm -rf "$root/usr/local/libexec/agent-pm"
  for service in agent-pm-tokens agent-pm-revocations; do
    count=0
    while delete_item "$service"; do
      count=$((count + 1))
      [ "$count" -lt 1000 ] || die "more than 1000 $service items; rerun"
    done
  done
  drop broker
}

# still <kind> <name>: after a failed remove, exit 0 when the client still lists it, 1 when it is gone;
# a list that cannot be read fails the Step, the record kept.
still() {
  local listing
  case $1 in
    codex-plugin) listing=$(codex plugin list --json </dev/null) ;;
    claude-plugin) listing=$(claude plugin list --json </dev/null) ;;
    claude-marketplace) listing=$(claude plugin marketplace list --json </dev/null) ;;
    codex-marketplace) listing=$(cat "${CODEX_HOME:-$HOME/.codex}/config.toml" 2>/dev/null || true) ;;
  esac || die "cannot list $1 to check $2"
  python3 -I -c '
import json, sys, tomllib
kind, name, text = sys.argv[1], sys.argv[2], sys.stdin.read()
try:
    if kind == "codex-marketplace":
        found = name in tomllib.loads(text).get("marketplaces", {})
    else:
        data = json.loads(text)
        items = data.get("installed", []) if kind == "codex-plugin" else data
        key = {"codex-plugin": "pluginId", "claude-plugin": "id", "claude-marketplace": "name"}[kind]
        found = any(item.get(key) == name for item in items)
except (ValueError, AttributeError, tomllib.TOMLDecodeError):
    sys.exit(2)
sys.exit(0 if found else 1)' "$1" "$2" <<<"$listing" || {
    [ $? = 1 ] && return 1
    die "cannot read the $1 list to check $2"
  }
}

plugins() {
  local kind name
  step plugins
  while IFS=$'\t' read -r kind name; do
    [ -n "$name" ] || continue
    case $kind in
      codex-plugin) need codex && codex plugin remove "$name" </dev/null >&2 ;;
      codex-marketplace) need codex && codex plugin marketplace remove "$name" </dev/null >&2 ;;
      claude-plugin) need claude && claude plugin uninstall "$name" </dev/null >&2 ;;
      claude-marketplace) need claude && claude plugin marketplace remove "$name" </dev/null >&2 ;;
    esac || ! still "$kind" "$name" || die "removing $kind $name failed; its record stays for a rerun"
    drop "$kind"$'\t'"$name"
  done <<<"$(of_kind codex-plugin; of_kind codex-marketplace; of_kind claude-plugin; of_kind claude-marketplace)"
}

logins() {
  local client
  step logins
  while IFS=$'\t' read -r _ client; do
    [ -n "$client" ] || continue
    need "$client"
    case $client in
      gh)
        if gh auth status </dev/null >/dev/null 2>&1; then gh auth logout --hostname github.com <&3 >&3 2>&3 || true; fi
        ! gh auth status </dev/null >/dev/null 2>&1 || die "gh is still logged in"
        ;;
      claude)
        if claude auth status </dev/null >/dev/null 2>&1; then claude auth logout <&3 >&3 2>&3 || true; fi
        ! claude auth status </dev/null >/dev/null 2>&1 || die "claude is still logged in"
        ;;
      codex)
        if codex login status </dev/null >/dev/null 2>&1; then codex logout <&3 >&3 2>&3 || true; fi
        ! codex login status </dev/null >/dev/null 2>&1 || die "codex is still logged in"
        ;;
    esac
    drop "login"$'\t'"$client"
  done <<<"$(of_kind login)"
}

opencode_keys() {
  local path name
  step "OpenCode keys"
  while IFS=$'\t' read -r _ path name; do
    [ -n "$name" ] || continue
    python3 -I -c '
import json, os, sys, tempfile
path, name = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    sys.exit()
except ValueError:
    sys.exit(f"install-cleanup.sh: {path} is not JSON; nothing written")
if name not in data:
    sys.exit()
del data[name]
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".auth.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
except BaseException:
    os.unlink(temp)
    raise' "$path" "$name"
    drop "opencode-auth"$'\t'"$path"$'\t'"$name"
  done <<<"$(of_kind opencode-auth)"
}

profiles() {
  local file line
  step "shell profiles"
  while IFS=$'\t' read -r _ file line; do
    [ -n "$file" ] || continue
    if [ -f "$file" ]; then
      grep -vxF -- "$line" "$file" >"$file.tmp" || true
      cat "$file.tmp" >"$file"
      rm -f "$file.tmp"
    fi
    drop "profile"$'\t'"$file"$'\t'"$line"
  done <<<"$(of_kind profile)"
  while IFS=$'\t' read -r _ file; do
    [ -n "$file" ] || continue
    if [ -f "$file" ] && ! grep -q '[^[:space:]]' "$file"; then rm -f "$file"; fi
    drop "profile-file"$'\t'"$file"
  done <<<"$(of_kind profile-file)"
}

config_file() {
  local backup=$state_dir/config.local.toml.orig
  step config.local.toml
  if grep -qx "config"$'\t'"created" "$ledger"; then
    rm -f "$config"
    drop "config"$'\t'"created"
  fi
  if grep -qx "config"$'\t'"backup" "$ledger"; then
    [ -f "$backup" ] || die "no $backup to restore"
    cp "$backup" "$config.tmp"
    mv "$config.tmp" "$config"
    drop "config"$'\t'"backup"
  fi
}

brew_packages() {
  local kind name reason
  step "Homebrew packages"
  if [ -n "$(of_kind cask)$(of_kind formula)" ]; then need brew; fi
  while IFS=$'\t' read -r kind name; do
    [ -n "$name" ] || continue
    if [ "$kind" = cask ]; then
      if brew list --cask "$name" </dev/null >/dev/null 2>&1; then brew uninstall --cask "$name" </dev/null >&2; fi
    elif brew list --formula "$name" </dev/null >/dev/null 2>&1; then
      if ! reason=$(brew uninstall "$name" </dev/null 2>&1); then
        case $reason in
          *"required by"*) say "    kept $name: $reason" ;;
          *) die "brew uninstall $name failed: $reason" ;;
        esac
      fi
    fi
    drop "$kind"$'\t'"$name"
  done <<<"$(grep -E '^(cask|formula)'$'\t' "$ledger" | sed -n '1!G;h;$p' || true)"
}

homebrew_and_clt() {
  local prefix uninstaller
  step "Homebrew and the Command Line Tools"
  prefix=$(of_kind homebrew | cut -f2)
  if [ -n "$prefix" ]; then
    uninstaller=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)
    NONINTERACTIVE=1 bash -c "$uninstaller" </dev/null >&2
    sudo -A rm -rf "$prefix"
    drop "homebrew"$'\t'"$prefix"
  fi
  if grep -qx clt "$ledger"; then
    sudo -A rm -rf "$root/Library/Developer/CommandLineTools"
    drop clt
  fi
}

files_and_dirs() {
  local path
  step "files and directories"
  while IFS=$'\t' read -r _ path; do
    [ -n "$path" ] || continue
    rm -f "$path"
    drop "file"$'\t'"$path"
  done <<<"$(of_kind file)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$HOME/.agent-pm" ] && continue
    rm -rf "$path"
    drop "dir"$'\t'"$path"
  done <<<"$(of_kind dir | cut -f2 | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)"
}

main() {
  local platform agent_root=
  set +x
  parse_args "$@"
  platform=$(uname -sm)
  [ "$platform" = "Darwin arm64" ] || die "needs macOS on Apple silicon (Darwin arm64), not $platform"
  if [ ! -e "$root/dev/tty" ] || ! (: <>"$root/dev/tty") 2>/dev/null; then die "needs a terminal for its questions and logins"; fi
  exec 3<>"$root/dev/tty"
  if [ ! -f "$ledger" ]; then
    say "nothing recorded by install.sh; nothing to remove"
    exit 0
  fi
  validate
  show
  if [ -z "$yes" ] && ! ask_yn "Remove all of this?"; then die "nothing removed"; fi
  trap on_exit EXIT
  sudo_once
  if [ -x "$root/opt/homebrew/bin/brew" ]; then eval "$("$root/opt/homebrew/bin/brew" shellenv)"; fi
  if grep -qx "dir"$'\t'"$HOME/.agent-pm" "$ledger"; then agent_root=1; fi
  launchd_jobs
  keychain_items
  broker_files
  plugins
  logins
  opencode_keys
  profiles
  config_file
  brew_packages
  homebrew_and_clt
  files_and_dirs
  step "~/.agent-pm"
  if grep -vx "dir"$'\t'"$HOME/.agent-pm" "$ledger" | grep -q .; then
    die "records no step removed are left in $ledger"
  fi
  rm -rf "$state_dir"
  if [ -n "$agent_root" ]; then rm -rf "$HOME/.agent-pm"; fi
  current=
  say "Removed what install.sh added."
}

main "$@"
