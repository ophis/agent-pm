// Package broker owns the broker's keychain secrets: the Claude setup-token (login, access, injection into a container)
// and the GitHub App private keys (login).
// It never decides how a container starts: the launcher calls it through Credentials with an exec bound to one container.
package broker

import (
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"regexp"
	"slices"
	"strings"
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
	AppAccess(app, account string) (AppAccess, error)
}

// ACLEntry is one entry of a keychain item's access. Apps are the paths of the applications it trusts
// unless Any; Partitions is the partition list a partition_id entry holds.
type ACLEntry struct {
	Authorizations []string
	Any            bool
	Apps           []string
	Partitions     []string
}

// AppAccess is a private-key item's access, beside the broker's own path and partition ID.
type AppAccess struct {
	Entries           []ACLEntry
	Broker, Partition string
}

var readAuthorizations = []string{"any", "decrypt", "export_clear", "export_wrapped"}

// onlyBroker is nil when every entry that can read or export the item trusts only the broker, one of them
// lets it decrypt, and the item's one partition list holds only the broker's partition ID.
func onlyBroker(a AppAccess) error {
	partitionLists, decrypt := 0, false
	for _, e := range a.Entries {
		if slices.Contains(e.Authorizations, "partition_id") {
			partitionLists++
			if !slices.Equal(e.Partitions, []string{a.Partition}) {
				return fmt.Errorf("its partition list is %q, not [%q]", e.Partitions, a.Partition)
			}
		}
		if !slices.ContainsFunc(e.Authorizations, func(x string) bool { return slices.Contains(readAuthorizations, x) }) {
			continue
		}
		switch {
		case e.Any:
			return fmt.Errorf("its %s entry trusts any application", strings.Join(e.Authorizations, ","))
		case len(e.Apps) == 0:
			return fmt.Errorf("its %s entry trusts no application", strings.Join(e.Authorizations, ","))
		case !slices.Equal(e.Apps, []string{a.Broker}):
			return fmt.Errorf("its %s entry trusts %q", strings.Join(e.Authorizations, ","), e.Apps)
		}
		decrypt = decrypt || slices.Contains(e.Authorizations, "decrypt") || slices.Contains(e.Authorizations, "any")
	}
	switch {
	case partitionLists != 1:
		return fmt.Errorf("it has %d partition lists, not one", partitionLists)
	case !decrypt:
		return errors.New("no entry lets the broker read it")
	}
	return nil
}

// parseAccess reads the access text keychain_darwin.go writes: one record per line, each field lowercase hex
// so no stored string can forge a record. "self <path> <partition ID>" once; "acl <authorization>..." starts an
// entry, and "any", "app <path>" and "partition <ID>" add to the last one.
func parseAccess(text []byte) (AppAccess, error) {
	var a AppAccess
	self := false
	for line := range strings.Lines(string(text)) {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		values := make([]string, len(fields)-1)
		for i, f := range fields[1:] {
			b, err := hex.DecodeString(f)
			if err != nil {
				return AppAccess{}, fmt.Errorf("unreadable access record %q", fields[0])
			}
			values[i] = string(b)
		}
		var last *ACLEntry
		if len(a.Entries) > 0 {
			last = &a.Entries[len(a.Entries)-1]
		}
		switch kind := fields[0]; {
		case kind == "self" && len(values) == 2 && !self:
			a.Broker, a.Partition, self = values[0], values[1], true
		case kind == "acl":
			a.Entries = append(a.Entries, ACLEntry{Authorizations: values})
		case kind == "any" && len(values) == 0 && last != nil:
			last.Any = true
		case kind == "app" && len(values) == 1 && last != nil:
			last.Apps = append(last.Apps, values[0])
		case kind == "partition" && len(values) == 1 && last != nil:
			last.Partitions = append(last.Partitions, values[0])
		default:
			return AppAccess{}, fmt.Errorf("unexpected access record %q", kind)
		}
	}
	if !self {
		return AppAccess{}, errors.New("access lacks the broker's identity")
	}
	return a, nil
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
	access, err := k.AppAccess(app, account)
	if err != nil {
		return fmt.Errorf("keychain item %s: reading back its access failed: %w", app, err)
	}
	if err := onlyBroker(access); err != nil {
		return fmt.Errorf("keychain item %s does not trust only the broker: %w", app, err)
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
