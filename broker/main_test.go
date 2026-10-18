package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ophis/agent-pm/broker/internal/audit"
	"github.com/ophis/agent-pm/broker/internal/broker"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/fixtures"
	"github.com/ophis/agent-pm/broker/internal/github"
	"github.com/ophis/agent-pm/broker/internal/launcher"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type call struct{ command, arg string }

func recording(calls *[]call) app {
	result := func(name string) step {
		return func(_ context.Context, arg string, _ io.Writer) (any, error) {
			*calls = append(*calls, call{name, arg})
			return map[string]string{"ok": name}, nil
		}
	}
	build, workspaceRm := result("build"), result("workspace-rm")
	return app{build: func(ctx context.Context, arg string, _ []string, _ time.Duration, p io.Writer) (any, error) {
		return build(ctx, arg, p)
	},
		start: result("start"), close: result("close"),
		refresh: func(ctx context.Context, container, manifest string) (any, error) {
			return result("refresh")(ctx, container+" "+manifest, nil)
		},
		status:      func(ctx context.Context) (any, error) { return result("status")(ctx, "", nil) },
		workspaceRm: func(ctx context.Context, volume string, _ bool) (any, error) { return workspaceRm(ctx, volume, nil) }}
}

func invoke(a app, args ...string) (int, string, string) {
	var out, errOut bytes.Buffer
	code := run(context.Background(), args, &out, &errOut, a)
	return code, out.String(), errOut.String()
}

func TestVersion(t *testing.T) {
	code, out, errOut := invoke(app{}, "version")
	if code != 0 || out != `{"protocol_version":"14"}`+"\n" || errOut != "" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	for _, c := range fixtures.Cases(t, "version_output") {
		if c.Valid && !fixtures.SameJSON(t, []byte(out), c.Data) {
			t.Fatalf("%s: %s", c.File, out)
		}
	}
}

func TestInputFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "version_input", "build_input", "start_input", "refresh_input", "close_input", "status_input", "workspace_rm_input") {
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
				want := call{in.Argv[0], ""}
				switch in.Argv[0] {
				case "refresh":
					want.arg = in.Argv[2] + " " + in.Argv[4]
				case "version", "status":
				default:
					want.arg = in.Argv[2]
				}
				if errOut != "" || out == "" || (in.Argv[0] != "version" && (len(calls) != 1 || calls[0] != want)) {
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
		{"build", []string{"build", "--agent-pm", "/x", "--max-age"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-age", "-1"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-age", "1.5"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-age", "1234567890"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-age", "1", "x"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-ages", "1"}},
		{"build", []string{"build", "--agent-pm", "/x", "--plugin"}},
		{"build", []string{"build", "--agent-pm", "/x", "--plugin", "../x"}},
		{"build", []string{"build", "--agent-pm", "/x", "--plugin", "a@"}},
		{"build", []string{"build", "--agent-pm", "/x", "--max-age", "1", "--plugin", "a"}},
		{"start", []string{"start", "--manifest", "/bad\npath"}},
		{"start", []string{"start", "--manifest", "/bad\xffpath"}},
		{"close", []string{"close", "--container", "other"}},
		{"refresh", []string{"refresh", "--container", "agent-pm-x-0123456789abcdef0123456789abcdef"}},
		{"refresh", []string{"refresh", "--container", "other", "--manifest", "/m.json"}},
		{"refresh", []string{"refresh", "--manifest", "/m.json", "--container", "agent-pm-x-0123456789abcdef0123456789abcdef"}},
		{"refresh", []string{"refresh", "--container", "agent-pm-x-0123456789abcdef0123456789abcdef", "--manifest", "m.json"}},
		{"status", []string{"status", "x"}},
		{"close", []string{"close", "--container", "agent-pm-x-0123"}},
		{"version", []string{"version", "x"}},
		{"workspace-rm", []string{"workspace-rm"}},
		{"workspace-rm", []string{"workspace-rm", "--volume"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", ".hidden"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", "a/b"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", strings.Repeat("v", 256)}},
		{"workspace-rm", []string{"workspace-rm", "--volume", "v", "--forced"}},
		{"workspace-rm", []string{"workspace-rm", "--force", "--volume", "v"}},
		{"unknown", []string{"login"}},
		{"unknown", []string{"login", "github"}},
		{"unknown", []string{"login", "github", "agent-pm-other"}},
		{"unknown", []string{"login", "github", "agent-pm-engineer"}},
		{"unknown", []string{"login", "github", "agent-pm-docs"}},
		{"unknown", []string{"login", "github", "agent-pm-bot", "x"}},
		{"unknown", []string{"login", "claude", "x"}},
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

func TestLoginGitHubDispatch(t *testing.T) {
	for _, name := range []string{"agent-pm-bot"} {
		var got string
		a := app{loginGitHub: func(ctx context.Context, app string, out io.Writer) error {
			if ctx == nil {
				t.Error("no context")
			}
			got = app
			_, err := io.WriteString(out, "ok\n")
			return err
		}}
		if code, out, errOut := invoke(a, "login", "github", name); code != 0 || got != name || out != "ok\n" || errOut != "" {
			t.Fatalf("%s: exit %d app %q %q %q", name, code, got, out, errOut)
		}
	}
	a := app{loginGitHub: func(context.Context, string, io.Writer) error { return errors.New("no item") }}
	if code, out, errOut := invoke(a, "login", "github", "agent-pm-bot"); code != 1 || out != "" || errOut != "agent-pm-broker: no item\n" {
		t.Fatalf("exit %d %q %q", code, out, errOut)
	}
}

func TestCodexCommands(t *testing.T) {
	for _, command := range []string{"install-codex", "update-codex"} {
		called := false
		a := app{installCodex: func(ctx context.Context, version string, update bool) (any, error) {
			called = true
			if version != "0.162.1" || update != (command == "update-codex") {
				t.Fatal("wrong install arguments")
			}
			return map[string]string{"version": version}, nil
		}}
		code, out, errOut := invoke(a, command, "0.162.1")
		if code != 0 || !called || out != "{\"version\":\"0.162.1\"}\n" || errOut != "" {
			t.Fatalf("%d %q %q", code, out, errOut)
		}
		for _, args := range [][]string{{command}, {command, "latest"}, {command, "0.162.1", "extra"}, {command, "0.162.1;touch /tmp/no"}} {
			called = false
			code, _, _ := invoke(a, args...)
			if code != 2 || called {
				t.Fatal("invalid admin command dispatched")
			}
		}
	}
	called := false
	a := app{loginCodex: func(ctx context.Context, out io.Writer) error {
		called = true
		_, err := io.WriteString(out, "saved\n")
		return err
	}}
	if code, out, errOut := invoke(a, "login", "codex"); code != 0 || !called || out != "saved\n" || errOut != "" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a.loginCodex = func(context.Context, io.Writer) error { return errors.New("login failed") }
	if code, out, errOut := invoke(a, "login", "codex"); code != 1 || out != "" || errOut != "agent-pm-broker: login failed\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a.installCodex = func(context.Context, string, bool) (any, error) { return nil, errors.New("install failed") }
	if code, out, errOut := invoke(a, "install-codex", "0.162.1"); code != 1 || out != "" || errOut != "agent-pm-broker: install failed\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

func TestCodexStatusFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "status_output") {
		if !c.Valid {
			continue
		}
		var result launcher.StatusResult
		if err := json.Unmarshal(c.Data, &result); err != nil {
			t.Fatal(err)
		}
		var out bytes.Buffer
		if emit(&out, result) != 0 {
			t.Fatal("status encoding failed")
		}
		var original, encoded any
		if json.Unmarshal(c.Data, &original) != nil || json.Unmarshal(out.Bytes(), &encoded) != nil || !reflect.DeepEqual(original, encoded) {
			t.Fatalf("status changed %s", out.String())
		}
	}
}

func TestBuildMaxAge(t *testing.T) {
	for want, args := range map[time.Duration][]string{
		-1:                      nil,
		0:                       {"--max-age", "0"},
		7 * 24 * time.Hour:      {"--max-age", "604800"},
		999999999 * time.Second: {"--max-age", "999999999"},
	} {
		got := time.Duration(-2)
		a := app{build: func(_ context.Context, _ string, _ []string, maxAge time.Duration, _ io.Writer) (any, error) {
			got = maxAge
			return map[string]string{}, nil
		}}
		if code, _, errOut := invoke(a, append([]string{"build", "--agent-pm", "/pkg"}, args...)...); code != 0 || got != want {
			t.Fatalf("%q: exit %d max age %v want %v: %s", args, code, got, want, errOut)
		}
	}
}

func TestBuildPlugins(t *testing.T) {
	for _, c := range []struct {
		args    []string
		plugins []string
		maxAge  time.Duration
	}{
		{nil, nil, -1},
		{[]string{"--plugin", "autopilot"}, []string{"autopilot"}, -1},
		{[]string{"--plugin", "b", "--plugin", "a@m.x", "--max-age", "60"}, []string{"b", "a@m.x"}, time.Minute},
	} {
		var plugins []string
		maxAge := time.Duration(-2)
		a := app{build: func(_ context.Context, _ string, p []string, m time.Duration, _ io.Writer) (any, error) {
			plugins, maxAge = p, m
			return map[string]string{}, nil
		}}
		if code, _, errOut := invoke(a, append([]string{"build", "--agent-pm", "/pkg"}, c.args...)...); code != 0 || !slices.Equal(plugins, c.plugins) || maxAge != c.maxAge {
			t.Fatalf("%q: exit %d plugins %q max age %v: %s", c.args, code, plugins, maxAge, errOut)
		}
	}
}

func TestWorkspaceRmForce(t *testing.T) {
	for want, args := range map[bool][]string{false: {"workspace-rm", "--volume", "v"}, true: {"workspace-rm", "--volume", "v", "--force"}} {
		got := !want
		a := app{workspaceRm: func(_ context.Context, volume string, force bool) (any, error) {
			got = force
			return map[string]any{"volume": volume, "removed": true}, nil
		}}
		if code, out, errOut := invoke(a, args...); code != 0 || got != want || out != `{"removed":true,"volume":"v"}`+"\n" {
			t.Fatalf("%q: exit %d force %v: %q %s", args, code, got, out, errOut)
		}
	}
}

func TestChannels(t *testing.T) {
	failing := app{build: func(context.Context, string, []string, time.Duration, io.Writer) (any, error) {
		return nil, protocol.New(protocol.BuildFailed, "stage failed")
	}, close: func(context.Context, string, io.Writer) (any, error) {
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
	a := app{start: func(context.Context, string, io.Writer) (any, error) { panic("boom") }}
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

// AppAccess is an item trusting only the broker.
func (k *memKeychain) AppAccess(string, string) (broker.AppAccess, error) {
	return broker.AppAccess{Broker: "b", Partition: "p", Entries: []broker.ACLEntry{
		{Authorizations: []string{"decrypt"}, Apps: []string{"b"}},
		{Authorizations: []string{"partition_id"}, Partitions: []string{"p"}},
	}}, nil
}
func (k *memKeychain) Get() ([]byte, error) { return append([]byte(nil), k.token...), nil }

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
	case strings.HasPrefix(joined, "volume inspect"):
		return []byte(`{"agent-pm.kind":"ws","agent-pm.workspace":"/Users/example/src/project"}`), nil
	case strings.HasPrefix(joined, "container inspect"):
		return nil, &docker.CommandError{Exit: 1, Stderr: "No such container"}
	case strings.HasSuffix(joined, "--version"):
		if strings.Contains(joined, "codex --version") {
			return []byte("codex-cli 0.100.0\n"), nil
		}
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
			IsDir:  func(string) bool { return true },
			Safe:   func(string) error { return nil },
			Docker: d,
			Creds:  broker.Credentials{Keychain: &memKeychain{token: []byte(token)}},
			GitHub: broker.GitHub{Keychain: noSecrets{}, API: github.Client{Now: time.Now}, Lock: broker.FileLock(filepath.Join(t.TempDir(), "tokens.lock"))},
		}
		a := app{start: func(ctx context.Context, m string, _ io.Writer) (any, error) { return h.Start(ctx, m) }}
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

func TestBuildProgressGoesToStderr(t *testing.T) {
	result := map[string]any{"image": "agent-pm-worker:k", "digest": "sha256:d", "rebuilt": true}
	build := func(fail bool) app {
		return app{build: func(_ context.Context, _ string, _ []string, _ time.Duration, progress io.Writer) (any, error) {
			_, _ = io.WriteString(progress, "#1 [1/3] FROM base\n#2 DONE\n")
			if fail {
				return nil, protocol.New(protocol.BuildFailed, "stage failed")
			}
			return result, nil
		}}
	}
	code, out, errOut := invoke(build(false), "build", "--agent-pm", "/pkg")
	want, _ := json.Marshal(result)
	if code != 0 || out != string(want)+"\n" || errOut != "#1 [1/3] FROM base\n#2 DONE\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	code, out, errOut = invoke(build(true), "build", "--agent-pm", "/pkg")
	progress, envelope, _ := strings.Cut(errOut, "#2 DONE\n")
	if code != 8 || progress != "#1 [1/3] FROM base\n" {
		t.Fatalf("%d %q", code, errOut)
	}
	assertError(t, envelope, out, "build", protocol.BuildFailed)
}

func TestStatus(t *testing.T) {
	a := app{status: func(context.Context) (any, error) {
		return launcher.StatusResult{Revocations: []protocol.Pending{{Container: "c", Account: "ophis", ExpiresAt: "2026-10-17T13:00:00Z"}}}, nil
	}}
	code, out, errOut := invoke(a, "status")
	if code != 0 || errOut != "" || out != `{"revocations":[{"container":"c","account":"ophis","expires_at":"2026-10-17T13:00:00Z"}]}`+"\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a = app{status: func(context.Context) (any, error) { return nil, protocol.New(protocol.KeychainFailed, "x") }}
	code, out, errOut = invoke(a, "status")
	if code != 11 {
		t.Fatalf("%d %q", code, errOut)
	}
	assertError(t, errOut, out, "status", protocol.KeychainFailed)
}

// noSecrets is an empty broker-only keychain.
type noSecrets struct{}

func (noSecrets) Secret(string, string) ([]byte, error)  { return nil, broker.ErrNotFound }
func (noSecrets) PutSecret(string, string, []byte) error { return errors.New("read-only") }
func (noSecrets) DeleteSecret(string, string) error      { return nil }

// memSecrets is a broker-only keychain in memory.
type memSecrets map[string][]byte

func (m memSecrets) Secret(service, account string) ([]byte, error) {
	if b, ok := m[service+"/"+account]; ok {
		return append([]byte(nil), b...), nil
	}
	return nil, broker.ErrNotFound
}
func (m memSecrets) PutSecret(service, account string, data []byte) error {
	m[service+"/"+account] = append([]byte(nil), data...)
	return nil
}
func (m memSecrets) DeleteSecret(service, account string) error {
	delete(m, service+"/"+account)
	return nil
}

type appKey []byte

func (appKey) AppAccount(string) (string, error)       { return "5352992", nil }
func (k appKey) AppKey(string, string) ([]byte, error) { return append([]byte(nil), k...), nil } // jwt erases it

func TestAuditHoldsNoSecret(t *testing.T) {
	const claudeToken = "sk-ant-oat01-AUDIT-NEVER-LOG-ME"
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	issued := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/app/installations":
			_, _ = io.WriteString(w, `[{"id":7,"account":{"login":"ophis"}}]`)
		case r.Method == http.MethodPost:
			issued++
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprintf(w, `{"token":"ghs_AUDITTEST_%d","expires_at":"2099-01-01T00:00:00Z"}`, issued)
		case r.URL.Path == "/installation/repositories":
			_, _ = io.WriteString(w, `{"total_count":1,"repository_selection":"selected","repositories":[{"full_name":"ophis/agent-pm"}]}`)
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL)
		}
	}))
	defer srv.Close()
	manifest, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "m.json")
	if err := os.WriteFile(path, manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	var m struct{ Name, Generation string }
	_ = json.Unmarshal(manifest, &m)
	c := "agent-pm-" + m.Name + "-" + m.Generation
	log := filepath.Join(t.TempDir(), "Library/Logs/agent-pm/broker.jsonl")
	keys := memSecrets{"agent-pm-github-users/agent-pm-bot": []byte("343764394")}
	lock := broker.FileLock(filepath.Join(t.TempDir(), "tokens.lock"))
	for _, command := range []string{"start", "refresh", "close"} {
		rec := &audit.Recorder{Command: command, Now: time.Now, Log: audit.Log{Path: log}}
		if command != "start" {
			rec.Container = c
		}
		const home = "/Users/example"
		h := launcher.Host{
			Home: home, Endpoint: "unix://" + home + "/.docker/run/docker.sock",
			IsDir:  func(string) bool { return true },
			Safe:   func(string) error { return nil },
			Docker: &scripted{},
			Creds:  broker.Credentials{Keychain: &memKeychain{token: []byte(claudeToken)}},
			GitHub: broker.GitHub{Keychain: keys, Apps: appKey(pemKey), Lock: lock, Audit: rec,
				API: github.Client{Base: srv.URL, HTTP: srv.Client(), Now: time.Now}},
			Audit: rec,
		}
		switch command {
		case "start":
			_, err = h.Start(context.Background(), path)
		case "refresh":
			_, err = h.Refresh(context.Background(), c, path)
		case "close":
			_, err = h.Close(context.Background(), c)
		}
		rec.Flush(err)
		if err != nil {
			t.Fatalf("%s: %v", command, err)
		}
	}
	data, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"ghs_AUDITTEST", claudeToken, "PRIVATE KEY"} {
		if bytes.Contains(data, []byte(secret)) {
			t.Fatalf("log holds %q:\n%s", secret, data)
		}
	}
	var results []string
	for _, line := range bytes.Split(bytes.TrimSpace(data), []byte("\n")) {
		var l struct {
			Event       string
			Credentials []struct{ Kind, Result string }
		}
		if err := json.Unmarshal(line, &l); err != nil {
			t.Fatal(err)
		}
		for _, cr := range l.Credentials {
			results = append(results, l.Event+" "+cr.Kind+" "+cr.Result)
		}
	}
	want := []string{"start claude-setup-token issued", "start github-installation-token issued",
		"refresh github-installation-token renewed", "refresh github-installation-token revoked", "close github-installation-token revoked"}
	if !slices.Equal(results, want) || !bytes.Contains(data, []byte(`"repositories":["ophis/agent-pm"]`)) {
		t.Fatalf("%v\n%s", results, data)
	}
}
