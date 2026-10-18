package launcher

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/fixtures"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

var expiry = []protocol.Expiry{{Account: "ophis", ExpiresAt: "2026-10-17T13:00:00Z"}}

// fakeTokens records the launcher's Tokens calls; each err field fails its method.
type fakeTokens struct {
	calls                                []string
	ready, issue, refresh, revoke, retry error
	pending                              []protocol.Pending
	onRevoke                             func()
}

func (f *fakeTokens) Ready(app string) error { f.calls = append(f.calls, "ready "+app); return f.ready }
func (f *fakeTokens) Issue(_ context.Context, app, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	f.calls = append(f.calls, "issue "+app+" "+c)
	if f.issue != nil {
		return nil, f.issue
	}
	return expiry, exec([]string{"write"}, []byte("ghs_token"))
}
func (f *fakeTokens) Refresh(_ context.Context, app, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	f.calls = append(f.calls, "refresh "+app+" "+c)
	if f.refresh != nil {
		return nil, f.refresh
	}
	return expiry, exec([]string{"write"}, []byte("ghs_token"))
}
func (f *fakeTokens) Revoke(_ context.Context, c string) error {
	f.calls = append(f.calls, "revoke "+c)
	if f.onRevoke != nil {
		f.onRevoke()
	}
	return f.revoke
}
func (f *fakeTokens) Retry(context.Context) ([]protocol.Pending, error) {
	f.calls = append(f.calls, "retry")
	return f.pending, f.retry
}

func githubManifest(t *testing.T) string {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

func githubHost(w *world, tokens *fakeTokens) (Host, *fakeDocker) {
	h, d := startHost(w, &fakeCreds{})
	h.GitHub = tokens
	return h, d
}

func TestStartGitHub(t *testing.T) {
	tokens := &fakeTokens{}
	h, d := githubHost(&world{labels: goodLabels()}, tokens)
	got, err := h.Start(context.Background(), githubManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got.GitHub, expiry) || !slices.Equal(tokens.calls, []string{"ready agent-pm-bot", "retry", "issue agent-pm-bot " + container}) {
		t.Fatalf("%v %q", got.GitHub, tokens.calls)
	}
	last := d.calls[len(d.calls)-1]
	if !slices.Equal(last, []string{"exec", "--interactive", "--user", "agent", container, "write"}) || string(d.stdin[len(d.stdin)-1]) != "ghs_token" {
		t.Fatalf("write %q", last)
	}
	inject := slices.IndexFunc(d.calls, func(c []string) bool { return slices.Contains(c, "inject") })
	if inject < 0 || inject > len(d.calls)-2 {
		t.Fatal("tokens written before the Claude token")
	}
}

func TestStartGitHubNull(t *testing.T) {
	tokens := &fakeTokens{}
	h, _ := githubHost(&world{labels: goodLabels()}, tokens)
	got, err := h.Start(context.Background(), validManifest(t))
	if err != nil || got.GitHub == nil || len(got.GitHub) != 0 || !slices.Equal(tokens.calls, []string{"retry"}) {
		t.Fatalf("%v %v %q", got.GitHub, err, tokens.calls)
	}
}

func TestStartGitHubRejectedBeforeContainer(t *testing.T) {
	for _, code := range []protocol.Code{protocol.ManifestRejected, protocol.LoginRequired, protocol.KeychainFailed} {
		tokens := &fakeTokens{ready: protocol.New(code, "no")}
		h, d := githubHost(&world{labels: goodLabels()}, tokens)
		_, err := h.Start(context.Background(), githubManifest(t))
		if e := protoErr(t, err); e.Code != code || e.Cleanup != protocol.NotNeeded || hasCall(d, "run") || hasCall(d, "volume create") {
			t.Fatalf("%s: %+v %q", code, e, d.verbs())
		}
	}
}

func TestStartGitHubFailureRollsBack(t *testing.T) {
	for name, tc := range map[string]struct {
		err  error
		code protocol.Code
	}{
		"github":      {protocol.New(protocol.GitHubFailed, "GitHub request failed: GitHub answered HTTP 500."), protocol.GitHubFailed},
		"unreachable": {errors.Join(errors.New("x"), docker.ErrUnreachable), protocol.DockerUnreachable},
		"opaque":      {errors.New("writing a GitHub token into the container failed"), protocol.StartFailed},
	} {
		t.Run(name, func(t *testing.T) {
			h, d := githubHost(&world{labels: goodLabels()}, &fakeTokens{issue: tc.err})
			_, err := h.Start(context.Background(), githubManifest(t))
			e := protoErr(t, err)
			if e.Code != tc.code || e.Cleanup != protocol.Removed || !hasCall(d, "rm") {
				t.Fatalf("%+v", e)
			}
		})
	}
}

func TestStartRetryStderr(t *testing.T) {
	var progress strings.Builder
	h, _ := githubHost(&world{labels: goodLabels()}, &fakeTokens{retry: protocol.New(protocol.KeychainFailed, "unreadable")})
	h.Progress = &progress
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(progress.String(), "agent-pm-broker: start revocations skipped\nagent-pm-broker: start preflight ") {
		t.Fatalf("%q", progress.String())
	}
	progress.Reset()
	h, _ = githubHost(&world{labels: goodLabels(), runErr: errors.New("boom")}, &fakeTokens{retry: errors.New("x")})
	h.Progress = &progress
	if _, err := h.Start(context.Background(), validManifest(t)); err == nil || progress.Len() != 0 {
		t.Fatalf("%v %q", err, progress.String())
	}
}

func TestCloseRevokesFirst(t *testing.T) {
	var order []string
	d := &fakeDocker{respond: func(args []string) ([]byte, error) {
		order = append(order, args[0])
		return []byte(container + "\n"), nil
	}}
	tokens := &fakeTokens{onRevoke: func() { order = append(order, "revoke") }}
	got, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if err != nil || !got.Removed || !slices.Equal(order, []string{"revoke", "rm"}) {
		t.Fatalf("%+v %v %v", got, err, order)
	}
}

func TestCloseKeychainFails(t *testing.T) {
	d := &fakeDocker{respond: func([]string) ([]byte, error) { return []byte(container + "\n"), nil }}
	tokens := &fakeTokens{revoke: protocol.New(protocol.KeychainFailed, "The GitHub tokens could not be revoked; they expire within an hour.")}
	_, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if protoErr(t, err).Code != protocol.KeychainFailed || !hasCall(d, "rm") {
		t.Fatalf("%v %q", err, d.calls)
	}
}

func TestRefresh(t *testing.T) {
	tokens := &fakeTokens{}
	d := &fakeDocker{}
	h := mountHost()
	h.Docker, h.GitHub = d, tokens
	got, err := h.Refresh(context.Background(), container, githubManifest(t))
	if err != nil || got.Container != container || !slices.Equal(got.GitHub, expiry) {
		t.Fatalf("%+v %v", got, err)
	}
	if !slices.Equal(d.calls[0], []string{"exec", "--interactive", "--user", "agent", container, "write"}) {
		t.Fatalf("%q", d.calls)
	}
	other := "agent-pm-worker_1-ffffffffffffffffffffffffffffffff"
	for name, tc := range map[string]struct {
		container, manifest string
		tokens              *fakeTokens
		code                protocol.Code
	}{
		"other container": {other, githubManifest(t), &fakeTokens{}, protocol.ManifestRejected},
		"github null":     {container, validManifest(t), &fakeTokens{}, protocol.ManifestRejected},
		"missing":         {container, "/nonexistent/m.json", &fakeTokens{}, protocol.ManifestRejected},
		"no key":          {container, githubManifest(t), &fakeTokens{ready: protocol.New(protocol.LoginRequired, "x")}, protocol.LoginRequired},
		"no record":       {container, githubManifest(t), &fakeTokens{refresh: protocol.New(protocol.RefreshFailed, "x")}, protocol.RefreshFailed},
		"write":           {container, githubManifest(t), &fakeTokens{refresh: errors.New("opaque")}, protocol.RefreshFailed},
		"unreachable":     {container, githubManifest(t), &fakeTokens{refresh: docker.ErrUnreachable}, protocol.DockerUnreachable},
	} {
		host := mountHost()
		host.Docker, host.GitHub = &fakeDocker{}, tc.tokens
		_, err := host.Refresh(context.Background(), tc.container, tc.manifest)
		if protoErr(t, err).Code != tc.code {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestStatus(t *testing.T) {
	pending := []protocol.Pending{{Container: container, Account: "ophis", ExpiresAt: "2026-10-17T13:00:00Z"}}
	got, err := Host{GitHub: &fakeTokens{pending: pending}}.Status(context.Background())
	if err != nil || !slices.Equal(got.Revocations, pending) {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := (Host{GitHub: &fakeTokens{retry: protocol.New(protocol.KeychainFailed, "x")}}).Status(context.Background()); protoErr(t, err).Code != protocol.KeychainFailed {
		t.Fatal(err)
	}
}
