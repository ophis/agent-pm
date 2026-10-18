package launcher

import (
	"context"
	"os"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Refresh replaces container's GitHub tokens; path is the manifest it was started with.
func (h Host) Refresh(ctx context.Context, container, path string) (RefreshResult, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return RefreshResult{}, err
	}
	if m.GitHubApp == "" || "agent-pm-"+m.Name+"-"+m.Generation != container {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: refresh needs "+container+"'s manifest, with a github app.")
	}
	if err := h.GitHub.Ready(m.GitHubApp); err != nil {
		return RefreshResult{}, err
	}
	expiries, err := h.GitHub.Refresh(ctx, m.GitHubApp, container, h.agentExec(ctx, container))
	if err != nil {
		return RefreshResult{}, injected(err, protocol.RefreshFailed, "Writing a GitHub token into "+container+" failed.")
	}
	return RefreshResult{Container: container, GitHub: expiries}, nil
}

// Status retries the revocation backlog and lists what is still pending.
func (h Host) Status(ctx context.Context) (StatusResult, error) {
	pending, err := h.GitHub.Retry(ctx)
	if err != nil {
		return StatusResult{}, err
	}
	return StatusResult{Revocations: pending}, nil
}
