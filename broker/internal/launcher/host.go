// Package launcher builds worker images and starts and closes their containers.
// It never touches the keychain or the token: injection goes through Credentials.
package launcher

import (
	"context"
	"errors"
	"path/filepath"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Credentials is the broker package's side of start; the launcher never sees the token.
type Credentials interface {
	Ready() error // *protocol.Error: login_required or keychain_failed
	// Inject errors wrap docker.ErrUnreachable or are opaque, carrying no Docker stderr.
	Inject(ctx context.Context, container string, d docker.Runner) error
}

type Host struct {
	Home     string
	Endpoint string
	Roots    func() ([]string, error)
	RealPath func(string) (string, error)
	Safe     func(string) error
	Same     func(a, b string) bool
	Docker   docker.Runner
	Creds    Credentials
}

// NewHost wires the real filesystem; tests build Host with fakes.
func NewHost(home, endpoint string, d docker.Runner, creds Credentials) Host {
	return Host{
		Home:     home,
		Endpoint: endpoint,
		Creds:    creds,
		Roots:    func() ([]string, error) { return LoadRoots(home) },
		RealPath: filepath.EvalSymlinks,
		Safe:     realSafe,
		Same:     realSame,
		Docker:   d,
	}
}

type BuildResult struct {
	Image   string `json:"image"`
	Digest  string `json:"digest"`
	Rebuilt bool   `json:"rebuilt"`
}

type Clients struct {
	Claude string `json:"claude"`
	Codex  string `json:"codex"`
}

type Update struct {
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
}

type StartResult struct {
	Container   string  `json:"container"`
	Endpoint    string  `json:"endpoint"`
	ImageDigest string  `json:"image_digest"`
	Clients     Clients `json:"clients"`
	Update      Update  `json:"update"`
}

type CloseResult struct {
	Container string `json:"container"`
	Removed   bool   `json:"removed"`
}

// lastLine is Docker's diagnostic: the last stderr line that is not the CLI's --help trailer.
func lastLine(err error) string {
	var ce *docker.CommandError
	if !errors.As(err, &ce) {
		return err.Error()
	}
	lines := strings.Split(ce.Stderr, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line != "" && !strings.HasPrefix(line, "Run 'docker ") && !strings.HasPrefix(line, "See 'docker ") {
			return line
		}
	}
	return err.Error()
}

// failure classifies a Docker error: unreachable daemon, else code.
func failure(err error, code protocol.Code, message string) *protocol.Error {
	if errors.Is(err, docker.ErrUnreachable) {
		return protocol.New(protocol.DockerUnreachable, "Cannot reach Docker Desktop; start it and retry.")
	}
	return protocol.New(code, message)
}

// diagnosed is failure with Docker's diagnostic as cause, only for the steps protocol 1 names: build stages and run.
func diagnosed(err error, code protocol.Code, message string) *protocol.Error {
	e := failure(err, code, message)
	if e.Code == code {
		e.Cause = lastLine(err)
	}
	return e
}

func absent(err error) bool {
	var ce *docker.CommandError
	return errors.As(err, &ce) && (strings.Contains(ce.Stderr, "No such image") || strings.Contains(ce.Stderr, "No such object") || strings.Contains(ce.Stderr, "No such container"))
}
