package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/broker"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/fixtures"
	"github.com/ophis/agent-pm/broker/internal/launcher"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type call struct{ command, arg string }

func recording(calls *[]call) app {
	result := func(name string) func(context.Context, string) (any, error) {
		return func(_ context.Context, arg string) (any, error) {
			*calls = append(*calls, call{name, arg})
			return map[string]string{"ok": name}, nil
		}
	}
	return app{build: result("build"), start: result("start"), close: result("close")}
}

func invoke(a app, args ...string) (int, string, string) {
	var out, errOut bytes.Buffer
	code := run(context.Background(), args, &out, &errOut, a)
	return code, out.String(), errOut.String()
}

func TestVersion(t *testing.T) {
	code, out, errOut := invoke(app{}, "version")
	if code != 0 || out != `{"protocol_version":"2"}`+"\n" || errOut != "" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	for _, c := range fixtures.Cases(t, "version_output") {
		if c.Valid && !fixtures.SameJSON(t, []byte(out), c.Data) {
			t.Fatalf("%s: %s", c.File, out)
		}
	}
}

func TestInputFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "version_input", "build_input", "start_input", "close_input") {
		t.Run(c.File, func(t *testing.T) {
			var in struct{ Argv []string }
			if err := json.Unmarshal(c.Data, &in); err != nil {
				t.Fatal(err)
			}
			var calls []call
			code, out, errOut := invoke(recording(&calls), in.Argv...)
			if code != *c.Expected.ExitCode {
				t.Fatalf("exit %d want %d: %s", code, *c.Expected.ExitCode, errOut)
			}
			if c.Valid {
				if errOut != "" || out == "" || (in.Argv[0] != "version" && (len(calls) != 1 || calls[0] != call{in.Argv[0], in.Argv[2]})) {
					t.Fatalf("%q %q %v", out, errOut, calls)
				}
				return
			}
			assertError(t, errOut, out, in.Argv[0], protocol.Code(*c.Expected.ErrorCode))
			if len(calls) != 0 {
				t.Fatalf("dispatched %v", calls)
			}
		})
	}
}

func assertError(t *testing.T, errOut, out, command string, code protocol.Code) {
	t.Helper()
	var got struct {
		ExitCode int `json:"exit_code"`
		Error    struct {
			Command string        `json:"command"`
			Code    protocol.Code `json:"code"`
			Cleanup string        `json:"cleanup"`
		} `json:"error"`
	}
	if out != "" || strings.Count(errOut, "\n") != 1 || json.Unmarshal([]byte(errOut), &got) != nil {
		t.Fatalf("stdout %q stderr %q", out, errOut)
	}
	if got.Error.Command != command || got.Error.Code != code || got.ExitCode != protocol.Exit(code) || got.Error.Cleanup != protocol.NotNeeded {
		t.Fatalf("%+v", got)
	}
}

func TestUsageErrors(t *testing.T) {
	var calls []call
	a := recording(&calls)
	for _, tc := range []struct {
		command string
		args    []string
	}{
		{"unknown", nil},
		{"unknown", []string{"frobnicate"}},
		{"build", []string{"build", "--agent-pm"}},
		{"build", []string{"build", "--package", "/x"}},
		{"build", []string{"build", "--agent-pm", "relative"}},
		{"start", []string{"start", "--manifest", "/bad\npath"}},
		{"start", []string{"start", "--manifest", "/bad\xffpath"}},
		{"close", []string{"close", "--container", "other"}},
		{"close", []string{"close", "--container", "agent-pm-x-0123"}},
		{"version", []string{"version", "x"}},
	} {
		code, out, errOut := invoke(a, tc.args...)
		if code != 2 {
			t.Fatalf("%q: exit %d", tc.args, code)
		}
		assertError(t, errOut, out, tc.command, protocol.Usage)
	}
	if len(calls) != 0 {
		t.Fatalf("dispatched %v", calls)
	}
}

func TestChannels(t *testing.T) {
	failing := app{build: func(context.Context, string) (any, error) {
		return nil, protocol.New(protocol.BuildFailed, "stage failed")
	}, close: func(context.Context, string) (any, error) {
		return nil, errors.New("surprise")
	}}
	code, out, errOut := invoke(failing, "build", "--agent-pm", "/pkg")
	if code != 8 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "build", protocol.BuildFailed)
	code, out, errOut = invoke(failing, "close", "--container", "agent-pm-w-0123456789abcdef0123456789abcdef")
	if code != 70 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "close", protocol.InternalError)
}

func TestPanicIsInternalError(t *testing.T) {
	a := app{start: func(context.Context, string) (any, error) { panic("boom") }}
	code, out, errOut := invoke(a, "start", "--manifest", "/m.json")
	if code != 70 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "start", protocol.InternalError)
	if strings.Contains(errOut, "cause") || strings.Contains(errOut, "boom") {
		t.Fatal(errOut)
	}
}

func TestAccountHomeIgnoresHOME(t *testing.T) {
	t.Setenv("HOME", "/hostile")
	home, err := accountHome()
	if err != nil || home == "/hostile" || !strings.HasPrefix(home, "/") {
		t.Fatalf("%q %v", home, err)
	}
}

type memKeychain struct{ token []byte }

func (k *memKeychain) Exists() error        { return nil }
func (k *memKeychain) Get() ([]byte, error) { return append([]byte(nil), k.token...), nil }
func (k *memKeychain) Put(b []byte) error   { k.token = b; return nil }

type scripted struct {
	calls [][]string
	stdin [][]byte
	fail  string
}

func (s *scripted) Run(_ context.Context, args []string, stdin []byte) ([]byte, error) {
	s.calls = append(s.calls, args)
	s.stdin = append(s.stdin, append([]byte(nil), stdin...))
	joined := strings.Join(args, " ")
	if s.fail != "" && strings.Contains(joined, s.fail) {
		return nil, &docker.CommandError{Exit: 1, Stderr: "failed: " + string(stdin)}
	}
	switch {
	case strings.HasPrefix(joined, "image inspect"):
		return []byte(`[{"Id":"sha256:` + strings.Repeat("b", 64) + `","Config":{"Labels":{"agent-pm.key":"` + strings.Repeat("a", 64) +
			`","agent-pm.version":"1.2.3","agent-pm.agent-uid":"1001","agent-pm.agent-gid":"1002"}}}]`), nil
	case strings.HasPrefix(joined, "container inspect"):
		return nil, &docker.CommandError{Exit: 1, Stderr: "No such container"}
	case strings.HasSuffix(joined, "--version"):
		return []byte("1.0.0\n"), nil
	}
	return nil, nil
}

func TestTokenNeverLeaves(t *testing.T) {
	const token = "sk-ant-oat01-NEVER-PRINT-ME"
	manifest, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "m.json")
	if err := os.WriteFile(path, manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	const home = "/Users/example"
	for _, fail := range []string{"", "cat >", "codex --version"} {
		d := &scripted{fail: fail}
		h := launcher.Host{
			Home: home, Endpoint: "unix://" + home + "/.docker/run/docker.sock",
			Roots:    func() ([]string, error) { return []string{home + "/src"}, nil },
			RealPath: func(p string) (string, error) { return p, nil },
			Safe:     func(string) error { return nil },
			Same:     func(a, b string) bool { return a == b },
			Docker:   d,
			Creds:    broker.Credentials{Keychain: &memKeychain{token: []byte(token)}},
		}
		a := app{start: func(ctx context.Context, m string) (any, error) { return h.Start(ctx, m) }}
		_, out, errOut := invoke(a, "start", "--manifest", path)
		if strings.Contains(out+errOut, token) {
			t.Fatalf("token in output: %q %q", out, errOut)
		}
		injected := 0
		for i, c := range d.calls {
			if strings.Contains(strings.Join(c, " "), token) {
				t.Fatalf("token in argv %q", c)
			}
			if strings.Contains(string(d.stdin[i]), token) {
				injected++
			}
		}
		if injected != 1 && fail != "codex --version" {
			t.Fatalf("%q: token written %d times", fail, injected)
		}
	}
}
