package broker

import (
	"bytes"
	"errors"
	"fmt"
	"io"

	"github.com/ophis/agent-pm/broker/internal/launcher"
)

// An OpenCode provider's API key: service opencodeService, account the provider's name.
const opencodeService = "agent-pm-opencode"

// OpencodeKeychain holds the providers' API keys as broker-only items.
type OpencodeKeychain interface {
	SecretKeychain
	ItemTrust
}

// LoginOpencode reads provider's API key with read, checks it, stores it in a broker-only item (replacing an earlier
// one), makes the item trust only the broker (secureItem, asking password when needed) and proves start's no-dialog
// read returns the key. Neither out nor an error sees the key.
func LoginOpencode(provider string, read, password func() ([]byte, error), k OpencodeKeychain, out io.Writer) error {
	if !launcher.ValidProvider(provider) {
		return fmt.Errorf("invalid OpenCode provider name %q", provider)
	}
	key, err := read()
	defer erase(key)
	if err != nil {
		return fmt.Errorf("reading the API key failed: %w", err)
	}
	if err := checkSecret("API key", key); err != nil {
		return err
	}
	item, rerun := opencodeService+" "+provider, "rerun agent-pm-broker login opencode "+provider
	if err := k.PutSecret(opencodeService, provider, key); err != nil {
		return fmt.Errorf("keychain item %s: saving it failed: %w; %s", item, err, rerun)
	}
	if err := secureItem(opencodeService, provider, rerun+" to finish", k, password); err != nil {
		return err
	}
	saved, err := k.Secret(opencodeService, provider)
	defer erase(saved)
	if err == nil && !bytes.Equal(saved, key) {
		err = errors.New("it holds other bytes")
	}
	if err != nil {
		return fmt.Errorf("keychain item %s: reading it failed: %w; %s", item, err, rerun)
	}
	_, err = fmt.Fprintf(out, "OpenCode provider %s API key saved; keychain trusts only the broker\n", provider)
	return err
}
