package broker

import (
	"bytes"
	"errors"
	"slices"
	"strings"
	"testing"
)

const apiKey = "sk-strata-SECRET"

// fakeSecrets is broker-only items keyed service/account, beside one item's access (fakeApps'); readBack replaces
// what a read returns when set.
type fakeSecrets struct {
	fakeApps
	items    map[string][]byte
	readBack []byte
	readErr  error
	putErr   error
	puts     []string
	returned [][]byte
}

func (k *fakeSecrets) Secret(service, account string) ([]byte, error) {
	if k.readErr != nil {
		return nil, k.readErr
	}
	data, ok := k.items[service+"/"+account]
	if !ok {
		return nil, ErrNotFound
	}
	b := append([]byte(nil), data...)
	if k.readBack != nil {
		b = append([]byte(nil), k.readBack...)
	}
	k.returned = append(k.returned, b)
	return b, nil
}

func (k *fakeSecrets) PutSecret(service, account string, data []byte) error {
	k.puts = append(k.puts, service+"/"+account)
	if k.putErr != nil {
		return k.putErr
	}
	if k.items == nil {
		k.items = map[string][]byte{}
	}
	k.items[service+"/"+account] = append([]byte(nil), data...)
	return nil
}

func (k *fakeSecrets) DeleteSecret(service, account string) error {
	delete(k.items, service+"/"+account)
	return nil
}

// loginOpencode runs LoginOpencode for provider strata with key; it fails t when a secret reaches an error or output.
func loginOpencode(t *testing.T, k *fakeSecrets, ask *askOnce, key string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	read := []byte(key)
	err := LoginOpencode("strata", func() ([]byte, error) { return read, nil }, ask.read, k, &out)
	text := out.String()
	if err != nil {
		text += err.Error()
	}
	if key != "" && strings.Contains(text, key) || strings.Contains(text, keychainPass) {
		t.Fatalf("secret in %q", text)
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("key buffer not erased")
	}
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			t.Fatal("read buffer not erased")
		}
	}
	return out.String(), err
}

func TestLoginOpencode(t *testing.T) {
	k := &fakeSecrets{}
	ask := &askOnce{}
	out, err := loginOpencode(t, k, ask, apiKey)
	if err != nil || out != "OpenCode provider strata API key saved; keychain trusts only the broker\n" {
		t.Fatalf("%v %q", err, out)
	}
	if string(k.items["agent-pm-opencode/strata"]) != apiKey || !slices.Equal(k.puts, []string{"agent-pm-opencode/strata"}) {
		t.Fatalf("%q %q", k.items, k.puts)
	}
	if ask.calls != 0 || len(k.steps) != 0 {
		t.Fatalf("asked %d, steps %q", ask.calls, k.steps)
	}
}

func TestLoginOpencodeRefusesKey(t *testing.T) {
	for _, bad := range []string{"", "has space", "tab\there", "nul\x00", "new\nline", strings.Repeat("a", 16385)} {
		k := &fakeSecrets{}
		ask := &askOnce{}
		if _, err := loginOpencode(t, k, ask, bad); err == nil || len(k.puts) != 0 || ask.calls != 0 {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
	var out bytes.Buffer
	err := LoginOpencode("strata", func() ([]byte, error) { return nil, errors.New("needs an interactive terminal") }, (&askOnce{}).read, &fakeSecrets{}, &out)
	if err == nil || err.Error() != "reading the API key failed: needs an interactive terminal" {
		t.Fatalf("read error: %v", err)
	}
}

func TestLoginOpencodeRefusesProvider(t *testing.T) {
	for _, bad := range []string{"", "ollama", "-x", ".x", "a/b", "a b", strings.Repeat("p", 65)} {
		k := &fakeSecrets{}
		var out bytes.Buffer
		if err := LoginOpencode(bad, func() ([]byte, error) { return []byte(apiKey), nil }, (&askOnce{}).read, k, &out); err == nil || len(k.puts) != 0 {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
}

// The item PutSecret makes still needs its partition list: secureItem asks the password once, then the read proves it.
func TestLoginOpencodeTrustsOnlyBroker(t *testing.T) {
	for name, tc := range map[string]struct {
		access       *AppAccess
		partitionErr error
		steps        []string
		fails        string
	}{
		"partition list": {access: halfDone(), steps: []string{"partition agent-pm-opencode strata"}},
		"refused": {access: halfDone(), partitionErr: errors.New("keychain status -25293"), steps: []string{"partition agent-pm-opencode strata"},
			fails: "rerun agent-pm-broker login opencode strata to finish"},
	} {
		k := &fakeSecrets{fakeApps: fakeApps{access: tc.access, partitionErr: tc.partitionErr}}
		ask := &askOnce{}
		_, err := loginOpencode(t, k, ask, apiKey)
		if ask.calls != 1 || !slices.Equal(k.steps, tc.steps) {
			t.Fatalf("%s: asked %d, steps %q", name, ask.calls, k.steps)
		}
		if tc.fails == "" && err != nil || tc.fails != "" && (err == nil || !strings.HasSuffix(err.Error(), tc.fails) ||
			!strings.Contains(err.Error(), "keychain item agent-pm-opencode")) {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

func TestLoginOpencodeReadsBack(t *testing.T) {
	for name, tc := range map[string]struct {
		k     *fakeSecrets
		fails string
	}{
		"other bytes": {&fakeSecrets{readBack: []byte("sk-other")},
			"keychain item agent-pm-opencode strata: reading it failed: it holds other bytes; rerun agent-pm-broker login opencode strata"},
		"needs dialog": {&fakeSecrets{readErr: ErrNeedsDialog},
			"keychain item agent-pm-opencode strata: reading it failed: keychain status -25308; rerun agent-pm-broker login opencode strata"},
		"put fails": {&fakeSecrets{putErr: errors.New("keychain status -25299")},
			"keychain item agent-pm-opencode strata: saving it failed: keychain status -25299; rerun agent-pm-broker login opencode strata"},
	} {
		if _, err := loginOpencode(t, tc.k, &askOnce{}, apiKey); err == nil || err.Error() != tc.fails {
			t.Fatalf("%s: %v", name, err)
		}
	}
}
