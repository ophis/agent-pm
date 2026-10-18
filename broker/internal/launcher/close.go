package launcher

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/audit"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// remove deletes c and reports whether it existed: rm --force exits 0 for an absent container but echoes no name.
func (h Host) remove(ctx context.Context, c string) (bool, error) {
	out, err := h.Docker.Run(ctx, []string{"rm", "--force", c}, nil)
	if err != nil && docker.Absent(err) {
		return false, nil
	}
	return err == nil && strings.TrimSpace(string(out)) == c, err
}

// heldClient is the client credential c's secrets tmpfs holds, from its env's client home ("": unknown). Only for the
// audit: nothing else of the env is kept.
func (h Host) heldClient(ctx context.Context, c string) string {
	if h.Audit == nil {
		return ""
	}
	out, err := h.Docker.Run(ctx, []string{"container", "inspect", "--format", "{{json .Config.Env}}", c}, nil)
	var env []string
	if err != nil || json.Unmarshal(out, &env) != nil {
		return ""
	}
	for _, kv := range env {
		switch {
		case strings.HasPrefix(kv, "CLAUDE_CONFIG_DIR="):
			return "claude"
		case strings.HasPrefix(kv, "CODEX_HOME="):
			return "codex"
		}
	}
	return ""
}

// Close revokes the container's GitHub tokens, then deletes it and its tmpfs; an absent container is success.
// A revocation failure is reported after the deletion, so it never keeps a container alive.
func (h Host) Close(ctx context.Context, container string) (CloseResult, error) {
	h.Audit.Begin(container, nil)
	client := h.heldClient(ctx, container)
	revoked := h.GitHub.Revoke(ctx, container)
	removed, err := h.remove(ctx, container)
	if removed && client != "" {
		h.Audit.Add(container, clientCredential(client, audit.Withdrawn, nil), "")
	}
	if err != nil {
		return CloseResult{}, failure(err, protocol.CloseFailed, "Deleting container "+container+" failed.")
	}
	if revoked != nil {
		return CloseResult{}, injected(revoked, protocol.KeychainFailed, "Revoking the GitHub tokens of "+container+" failed.")
	}
	return CloseResult{Container: container, Removed: removed}, nil
}
