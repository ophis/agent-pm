// Package broker owns the Claude setup-token: login, keychain access and injection into a container.
// It never decides how a container starts: the launcher calls it through Credentials with an exec bound to one container.
package broker

import (
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

// keychainError maps a keychain failure to the protocol, without its detail.
func keychainError(err error) *protocol.Error {
	if errors.Is(err, ErrNotFound) {
		return protocol.New(protocol.LoginRequired, "No Claude setup-token: run agent-pm-broker login claude.")
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to the Claude setup-token failed.")
}

func (c Credentials) Ready() error {
	if err := c.Keychain.Exists(); err != nil {
		return keychainError(err)
	}
	return nil
}

// Inject writes the token into the container's secrets tmpfs through exec's stdin.
// The token read is the first that can prompt, so a denied or locked keychain fails here.
// Errors are keychainError's, wrap docker.ErrUnreachable or are opaque: no Docker stderr, no keychain detail.
func (c Credentials) Inject(exec func(argv []string, stdin []byte) error) error {
	token, err := c.Keychain.Get()
	defer erase(token)
	if err != nil {
		return keychainError(err)
	}
	err = exec([]string{"/bin/sh", "-c", "umask 0377 && set -C && cat > " + docker.SecretsDir + "/claude"}, token)
	if errors.Is(err, docker.ErrUnreachable) {
		return fmt.Errorf("token injection: %w", docker.ErrUnreachable)
	}
	if err != nil {
		return errors.New("writing the token into the container failed")
	}
	return nil
}
