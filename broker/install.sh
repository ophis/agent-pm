#!/bin/sh
# Installs agent-pm-broker root-owned and seeds the worker image definition once. Rerun to upgrade.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
target=/usr/local/libexec/agent-pm
worker="$HOME/.agent-pm/docker/worker"
if [ ! -e "$worker" ]; then
  mkdir -p "$worker"
  cp "$here/worker/Dockerfile" "$worker/Dockerfile"
  echo "Seeded $worker/Dockerfile"
fi
if ! command -v go >/dev/null 2>&1; then
  echo "install.sh: go is required to build agent-pm-broker (brew install go)" >&2
  exit 1
fi
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
(cd "$here" && CGO_ENABLED=1 go build -trimpath -o "$build/agent-pm-broker" .)
codesign --force --sign - --options runtime --identifier com.github.ophis.agent-pm-broker "$build/agent-pm-broker"
sudo install -d -o root -g wheel -m 0755 "$target"
sudo install -o root -g wheel -m 0755 "$build/agent-pm-broker" "$target/agent-pm-broker"
echo "Installed $target/agent-pm-broker"
echo "Upgrade: rerun sh $here/install.sh"
