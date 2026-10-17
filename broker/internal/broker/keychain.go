// Package broker owns the Claude setup-token: login, keychain access and injection into a container.
// It never decides how a container starts; the launcher calls it through Credentials.
package broker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"unicode"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const (
	InstalledPath  = "/usr/local/libexec/agent-pm/agent-pm-broker"
	UpgradeCommand = "sh <agent-pm plugin directory>/broker/install.sh"
	maxToken       = 16384
)

type Keychain interface {
	Exists() error
	Get() ([]byte, error)
	Put([]byte) error
}

var ErrNotFound = errors.New("setup-token item not found")

func erase(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// Login reads a setup-token with read, checks it and stores it; out never sees the token.
func Login(read func() ([]byte, error), k Keychain, out io.Writer) error {
	token, err := read()
	defer erase(token)
	if err != nil {
		return err
	}
	if len(token) == 0 || len(token) > maxToken {
		return fmt.Errorf("setup-token must be 1 to %d bytes", maxToken)
	}
	for _, r := range string(token) {
		if unicode.IsSpace(r) || unicode.IsControl(r) || r == unicode.ReplacementChar {
			return errors.New("setup-token contains whitespace or control characters")
		}
	}
	if err := k.Put(token); err != nil {
		return err
	}
	_, err = fmt.Fprintln(out, "Claude setup-token saved")
	return err
}

type Credentials struct{ Keychain Keychain }

func (c Credentials) Ready() error {
	err := c.Keychain.Exists()
	switch {
	case err == nil:
		return nil
	case errors.Is(err, ErrNotFound):
		return protocol.New(protocol.LoginRequired, "No Claude setup-token: run agent-pm-broker login claude.")
	default:
		e := protocol.New(protocol.KeychainFailed, "Keychain access to the Claude setup-token failed.")
		e.Cause = err.Error()
		return e
	}
}

// Inject writes the token into the container's secrets tmpfs through exec stdin.
// Errors wrap docker.ErrUnreachable or are opaque: no Docker stderr, no keychain detail.
func (c Credentials) Inject(ctx context.Context, container string, d docker.Runner) error {
	token, err := c.Keychain.Get()
	defer erase(token)
	if err != nil {
		return errors.New("Claude setup-token unavailable for injection")
	}
	_, err = d.Run(ctx, []string{"exec", "--interactive", "--user", "agent", container,
		"/bin/sh", "-c", "umask 0377 && set -C && cat > " + docker.SecretsDir + "/claude"}, token)
	if errors.Is(err, docker.ErrUnreachable) {
		return fmt.Errorf("token injection: %w", docker.ErrUnreachable)
	}
	if err != nil {
		return errors.New("writing the token into the container failed")
	}
	return nil
}
