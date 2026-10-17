// Package broker owns the broker's keychain secrets: the Claude setup-token (login, access, injection into a container)
// and the GitHub App private keys (login).
// It never decides how a container starts: the launcher calls it through Credentials with an exec bound to one container.
package broker

import (
	"errors"
	"fmt"
	"io"
	"regexp"
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

var (
	ErrNotFound  = errors.New("keychain item not found")
	ErrAmbiguous = errors.New("more than one keychain item")
)

// GitHubApps are the keychain services of the GitHub App private keys login github accepts.
var GitHubApps = map[string]bool{"agent-pm-engineer": true, "agent-pm-docs": true}

// AppKeychain holds GitHub App private keys as generic-password items: service the App, account its App ID.
type AppKeychain interface {
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
	TrustOnlyBroker(app, account string) error
}

var appID = regexp.MustCompile(`^[1-9][0-9]{0,19}$`)

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

// LoginGitHub reads app's existing private-key item once to confirm it is usable, then changes its ACL in place
// to trust only the broker; the item is never deleted and its content never changes. out never sees the key.
func LoginGitHub(app string, k AppKeychain, out io.Writer) error {
	account, err := k.AppAccount(app)
	switch {
	case errors.Is(err, ErrNotFound):
		return fmt.Errorf("no keychain item with service %s: add the App private key first", app)
	case errors.Is(err, ErrAmbiguous):
		return fmt.Errorf("more than one keychain item with service %s: keep only the App private key", app)
	case err != nil:
		return err
	case !appID.MatchString(account):
		return fmt.Errorf("keychain item %s: account %q is not an App ID", app, account)
	}
	key, err := k.AppKey(app, account)
	empty := len(key) == 0
	erase(key)
	switch {
	case err != nil:
		return err
	case empty:
		return fmt.Errorf("keychain item %s is empty", app)
	}
	if err := k.TrustOnlyBroker(app, account); err != nil {
		return fmt.Errorf("keychain item %s unchanged: changing its access failed: %w", app, err)
	}
	_, err = fmt.Fprintf(out, "GitHub App %s private key (App ID %s) now trusts only the broker\n", app, account)
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
