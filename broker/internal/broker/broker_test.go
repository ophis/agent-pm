package broker

import (
	"bytes"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type fakeKeychain struct {
	token          []byte
	exists, getErr error
	put            []byte
	gets           int
}

func (k *fakeKeychain) Exists() error { return k.exists }
func (k *fakeKeychain) Get() ([]byte, error) {
	k.gets++
	return append([]byte(nil), k.token...), k.getErr
}
func (k *fakeKeychain) Put(b []byte) error { k.put = append([]byte(nil), b...); return nil }

type fakeExec struct {
	argv  [][]string
	stdin [][]byte
	err   error
}

func (e *fakeExec) run(argv []string, stdin []byte) error {
	e.argv = append(e.argv, argv)
	e.stdin = append(e.stdin, append([]byte(nil), stdin...))
	return e.err
}

const token = "sk-ant-oat01-SECRET"

func TestLogin(t *testing.T) {
	for _, bad := range []string{"", "has space", "tab\there", "nul\x00", strings.Repeat("a", 16385)} {
		k := &fakeKeychain{}
		var out bytes.Buffer
		err := Login(func() ([]byte, error) { return []byte(bad), nil }, k, &out)
		if err == nil || k.put != nil || strings.Contains(err.Error(), bad) && bad != "" {
			t.Fatalf("%q accepted or echoed: %v", bad, err)
		}
	}
	k := &fakeKeychain{}
	var out bytes.Buffer
	read := []byte(token)
	if err := Login(func() ([]byte, error) { return read, nil }, k, &out); err != nil {
		t.Fatal(err)
	}
	if string(k.put) != token || out.String() != "Claude setup-token saved\n" {
		t.Fatalf("%q %q", k.put, out.String())
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("token buffer not erased")
	}
	if err := Login(func() ([]byte, error) { return nil, errors.New("no tty") }, &fakeKeychain{}, &out); err == nil {
		t.Fatal("read error ignored")
	}
}

type fakeApps struct {
	account, key       string
	accountErr, getErr error
	putErr             error
	put                []string
	got                []byte
}

func (k *fakeApps) AppAccount(app string) (string, error) { return k.account, k.accountErr }
func (k *fakeApps) AppKey(app, account string) ([]byte, error) {
	if account != k.account {
		return nil, ErrNotFound
	}
	k.got = []byte(k.key)
	return k.got, k.getErr
}
func (k *fakeApps) PutAppKey(app, account string, key []byte) error {
	if k.putErr != nil {
		return k.putErr
	}
	k.put = []string{app, account, string(key)}
	k.key = string(key)
	return nil
}

func TestLoginGitHub(t *testing.T) {
	const key = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ=="
	k := &fakeApps{account: "5352992", key: key}
	for range 2 {
		var out bytes.Buffer
		if err := LoginGitHub("agent-pm-engineer", k, &out); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(k.put, []string{"agent-pm-engineer", "5352992", key}) {
			t.Fatalf("put %q", k.put)
		}
		if out.String() != "GitHub App agent-pm-engineer private key (App ID 5352992) now trusts only the broker\n" {
			t.Fatalf("%q", out.String())
		}
		if !bytes.Equal(k.got, make([]byte, len(key))) {
			t.Fatal("key buffer not erased")
		}
	}
	for name, k := range map[string]*fakeApps{
		"missing":     {accountErr: ErrNotFound},
		"ambiguous":   {accountErr: ErrAmbiguous},
		"keychain":    {accountErr: errors.New("keychain status -25308")},
		"not an id":   {account: "setup-token", key: key},
		"zero id":     {account: "0123", key: key},
		"read failed": {account: "5352992", key: key, getErr: errors.New("keychain status -128")},
		"empty":       {account: "5352992"},
		"put failed":  {account: "5352992", key: key, putErr: errors.New("item deleted but not re-created")},
	} {
		var out bytes.Buffer
		err := LoginGitHub("agent-pm-docs", k, &out)
		if err == nil || k.put != nil || out.Len() != 0 || strings.Contains(err.Error(), key) {
			t.Fatalf("%s: %v put %q out %q", name, err, k.put, out.String())
		}
		if k.got != nil && !bytes.Equal(k.got, make([]byte, len(k.got))) {
			t.Fatalf("%s: key buffer not erased", name)
		}
	}
}

func TestReady(t *testing.T) {
	for _, tc := range []struct {
		err  error
		code protocol.Code
	}{
		{nil, ""},
		{ErrNotFound, protocol.LoginRequired},
		{errors.New("user denied"), protocol.KeychainFailed},
	} {
		k := &fakeKeychain{exists: tc.err}
		err := Credentials{Keychain: k}.Ready()
		var e *protocol.Error
		if tc.code == "" && err != nil || tc.code != "" && (!errors.As(err, &e) || e.Code != tc.code) {
			t.Fatalf("%v → %v", tc.err, err)
		}
		if tc.code == protocol.LoginRequired && !strings.Contains(e.Message, "agent-pm-broker login claude") {
			t.Fatal(e.Message)
		}
		if k.gets != 0 {
			t.Fatal("Ready read the token")
		}
		if e != nil && e.Cause != "" {
			t.Fatalf("keychain detail in cause: %q", e.Cause)
		}
	}
}

func TestInject(t *testing.T) {
	e := &fakeExec{}
	k := &fakeKeychain{token: []byte(token)}
	if err := (Credentials{Keychain: k}).Inject(e.run); err != nil {
		t.Fatal(err)
	}
	want := []string{"/bin/sh", "-c", "umask 0377 && set -C && cat > " + docker.SecretsDir + "/claude"}
	if !reflect.DeepEqual(e.argv, [][]string{want}) || string(e.stdin[0]) != token {
		t.Fatalf("%q %q", e.argv, e.stdin)
	}
	for _, tc := range []struct {
		dockerErr, keyErr error
		unreachable       bool
	}{
		{&docker.CommandError{Exit: 1, Stderr: "cat: " + token}, nil, false},
		{errors.Join(docker.ErrUnreachable, errors.New(token)), nil, true},
	} {
		e := &fakeExec{err: tc.dockerErr}
		err := Credentials{Keychain: &fakeKeychain{token: []byte(token), getErr: tc.keyErr}}.Inject(e.run)
		if err == nil || strings.Contains(err.Error(), token) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable {
			t.Fatalf("%v", err)
		}
		var ce *docker.CommandError
		if errors.As(err, &ce) {
			t.Fatal("Docker stderr kept")
		}
	}
}

func TestInjectKeychainFailure(t *testing.T) {
	for keyErr, code := range map[error]protocol.Code{errors.New("locked " + token): protocol.KeychainFailed, ErrNotFound: protocol.LoginRequired} {
		e := &fakeExec{}
		err := Credentials{Keychain: &fakeKeychain{getErr: keyErr}}.Inject(e.run)
		var pe *protocol.Error
		if !errors.As(err, &pe) || pe.Code != code || pe.Cause != "" || strings.Contains(err.Error(), token) || len(e.argv) != 0 {
			t.Fatalf("%v: %v %q", keyErr, err, e.argv)
		}
	}
}
