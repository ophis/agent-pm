// Package launcher builds worker images and starts and closes their containers.
// It never touches the keychain or the token: injection goes through Credentials.
package launcher

import (
	"errors"
	"io"
	"path/filepath"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Credentials is the broker package's side of start; the launcher never sees the token.
type Credentials interface {
	Ready() error // *protocol.Error: login_required or keychain_failed
	// Inject writes the token through exec, which runs argv as agent in the container being started.
	// Its errors are *protocol.Error (login_required, keychain_failed), wrap docker.ErrUnreachable
	// or are opaque, carrying no Docker stderr.
	Inject(exec func(argv []string, stdin []byte) error) error
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
	Progress io.Writer // nil: none; build's notes on older images, after Docker's progress; start's phase times
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

type StartResult struct {
	Container   string  `json:"container"`
	Endpoint    string  `json:"endpoint"`
	ImageDigest string  `json:"image_digest"`
	Clients     Clients `json:"clients"`
}

type CloseResult struct {
	Container string `json:"container"`
	Removed   bool   `json:"removed"`
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
		e.Cause = docker.Diagnostic(err)
	}
	return e
}
