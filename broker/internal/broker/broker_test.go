package broker

import (
	"bytes"
	"context"
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

type fakeDocker struct {
	args  [][]string
	stdin [][]byte
	err   error
}

func (d *fakeDocker) Run(_ context.Context, args []string, stdin []byte) ([]byte, error) {
	d.args = append(d.args, args)
	d.stdin = append(d.stdin, append([]byte(nil), stdin...))
	return nil, d.err
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
	d := &fakeDocker{}
	k := &fakeKeychain{token: []byte(token)}
	if err := (Credentials{Keychain: k}).Inject(context.Background(), "agent-pm-w-0123", d); err != nil {
		t.Fatal(err)
	}
	want := []string{"exec", "--interactive", "--user", "agent", "agent-pm-w-0123", "/bin/sh", "-c", "umask 0377 && set -C && cat > " + docker.SecretsDir + "/claude"}
	if !reflect.DeepEqual(d.args, [][]string{want}) || string(d.stdin[0]) != token {
		t.Fatalf("%q %q", d.args, d.stdin)
	}
	for _, tc := range []struct {
		dockerErr, keyErr error
		unreachable       bool
	}{
		{&docker.CommandError{Exit: 1, Stderr: "cat: " + token}, nil, false},
		{errors.Join(docker.ErrUnreachable, errors.New(token)), nil, true},
		{nil, errors.New("locked " + token), false},
	} {
		d := &fakeDocker{err: tc.dockerErr}
		err := Credentials{Keychain: &fakeKeychain{token: []byte(token), getErr: tc.keyErr}}.Inject(context.Background(), "c", d)
		if err == nil || strings.Contains(err.Error(), token) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable {
			t.Fatalf("%v", err)
		}
		var ce *docker.CommandError
		if errors.As(err, &ce) {
			t.Fatal("Docker stderr kept")
		}
	}
}
