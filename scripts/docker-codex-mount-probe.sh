#!/bin/bash
set -euo pipefail

if [ "${GITHUB_ACTIONS:-}" != true ] && [ "${AGENT_PM_CODEX_USER_PROBE:-}" != 1 ]; then
    printf '%s\n' '{"status":"refused","error":"ci_or_user_only"}'
    exit 2
fi

probe_root=$(mktemp -d "${TMPDIR:-/tmp}/task-647-mount.XXXXXX")
probe_name="task-647-mount-$(basename "$probe_root" | tr '[:upper:]' '[:lower:]')"
cleanup() {
    docker rm -f "$probe_name" >/dev/null 2>&1 || true
    rm -rf "${probe_root:?}"
}
trap cleanup EXIT
mkdir "$probe_root/sessions"
probe_uid=$(id -u)
probe_gid=$(id -g)
probe_image=alpine:3.22.2
docker pull "$probe_image" >"$probe_root/pull.log" 2>&1
docker version --format '{{json .Server.Version}}'
docker image inspect "$probe_image" --format '{{json .RepoDigests}}'

docker run --name "$probe_name" --network none --read-only \
    --user "$probe_uid:$probe_gid" --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs "/probe/home:rw,noexec,nosuid,nodev,size=4m,mode=0700,uid=$probe_uid,gid=$probe_gid" \
    --mount "type=bind,src=$probe_root/sessions,dst=/probe/home/sessions" \
    "$probe_image" sh -eu -c '
        test ! -e /probe/home/auth.json
        printf synthetic-secret > /probe/home/auth.json
        printf synthetic-replacement > /probe/home/auth.next
        mv /probe/home/auth.next /probe/home/auth.json
        printf synthetic-history > /probe/home/sessions/rollout
        test "$(cat /probe/home/auth.json)" = synthetic-replacement
    ' >"$probe_root/run.log" 2>&1

test "$(cat "$probe_root/sessions/rollout")" = synthetic-history
test "$(find "$probe_root/sessions" -type f | wc -l | tr -d ' ')" = 1
if grep -r -q 'synthetic-secret\|synthetic-replacement' "$probe_root/sessions"; then
    printf '%s\n' '{"status":"failed","error":"persistent_secret"}'
    exit 1
fi
docker start -a "$probe_name" >"$probe_root/restart.log" 2>&1
test "$(docker inspect "$probe_name" --format '{{.State.ExitCode}}')" = 0
docker rm "$probe_name" >"$probe_root/remove.log" 2>&1
printf '%s\n' '{"status":"pass","count":2,"scope":"synthetic_mount_restart_only"}'
