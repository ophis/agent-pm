package launcher

import (
	"context"
	"strings"

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

// Close deletes the container and its tmpfs; an absent container is success.
func (h Host) Close(ctx context.Context, container string) (CloseResult, error) {
	removed, err := h.remove(ctx, container)
	if err != nil {
		return CloseResult{}, failure(err, protocol.CloseFailed, "Deleting container "+container+" failed.")
	}
	return CloseResult{Container: container, Removed: removed}, nil
}
