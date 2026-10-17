package launcher

import (
	"context"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Close deletes the container and its tmpfs; an absent container is success.
func (h Host) Close(ctx context.Context, container string) (CloseResult, error) {
	_, err := h.Docker.Run(ctx, []string{"rm", "--force", container}, nil)
	switch {
	case err == nil:
		return CloseResult{Container: container, Removed: true}, nil
	case absent(err):
		return CloseResult{Container: container}, nil
	}
	return CloseResult{}, failure(err, protocol.CloseFailed, "Deleting container "+container+" failed.")
}
