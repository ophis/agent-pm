package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"

	"github.com/ophis/agent-pm/broker/internal/audit"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Refresh replaces container credentials; path is its original manifest.
func (h Host) Refresh(ctx context.Context, container, path string) (RefreshResult, error) {
	h.Audit.Begin(container, nil)
	data, err := os.ReadFile(path)
	if err != nil {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return RefreshResult{}, err
	}
	if "agent-pm-"+m.Name+"-"+m.Generation != container || (m.GitHubApp == "" && m.Client != "codex") {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: refresh needs the container's manifest and renewable credentials.")
	}
	if err := h.checkMounts(m); err != nil {
		return RefreshResult{}, err
	}
	result := RefreshResult{Container: container, GitHub: []protocol.Expiry{}}
	if m.Client == "codex" {
		exec := h.agentExec(ctx, container)
		inject := func(argv []string, data []byte) error {
			return exec(append(argv, m.CodexDir), data)
		}
		expiry, err := h.Codex.Refresh(ctx, inject, func(expiry string) error {
			if err := h.publishExpiry(m, expiry); err != nil {
				return protocol.New(protocol.RefreshFailed, "Publishing the Codex credential deadline failed; retry refresh.")
			}
			return nil
		})
		if err != nil {
			return RefreshResult{}, injected(err, protocol.RefreshFailed, "Writing Codex credentials into "+container+" failed.")
		}
		result.CredentialExpiresAt = &expiry
		h.Audit.Add(container, audit.Credential{Kind: clientKind(m.Client), Result: audit.Renewed, ExpiresAt: &expiry}, "")
	}
	if m.GitHubApp != "" {
		if err := h.GitHub.Ready(m.GitHubApp); err != nil {
			return RefreshResult{}, err
		}
		expiries, err := h.GitHub.Refresh(ctx, m.GitHubApp, container, h.agentExec(ctx, container))
		if err != nil {
			return RefreshResult{}, injected(err, protocol.RefreshFailed, "Writing a GitHub token into "+container+" failed.")
		}
		result.GitHub = expiries
	}
	return result, nil
}

func (h Host) publishExpiry(m Manifest, expiry string) error {
	value, err := time.Parse(time.RFC3339, expiry)
	if err != nil || value.UTC().Format(time.RFC3339) != expiry {
		return errors.New("invalid credential deadline")
	}
	parent := filepath.Join(h.Home, ".agent-pm/docker-state", m.Name)
	if err := h.Safe(parent); err != nil {
		return err
	}
	data, err := json.Marshal(expiry)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(parent, ".credential-")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	destination := filepath.Join(parent, m.Generation+".credential.json")
	if err := os.Rename(temporary.Name(), destination); err != nil {
		return err
	}
	directory, err := os.Open(parent)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

// Status retries the revocation backlog and lists what is still pending.
func (h Host) Status(ctx context.Context) (StatusResult, error) {
	pending, err := h.GitHub.Retry(ctx)
	if err != nil {
		return StatusResult{}, err
	}
	return StatusResult{Revocations: pending}, nil
}
