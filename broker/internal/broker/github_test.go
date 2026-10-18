package broker

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/github"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const ctr = "agent-pm-worker_1-0123456789abcdef0123456789abcdef"

var (
	clock    = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	keyOnce  sync.Once
	appKeyB  []byte
	errLeaky = errors.New("keychain status -25293")
)

func testAppKey(t *testing.T) []byte {
	t.Helper()
	keyOnce.Do(func() {
		key, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			t.Fatal(err)
		}
		pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
		appKeyB = []byte(base64.StdEncoding.EncodeToString(pemKey))
	})
	return appKeyB
}

// secrets is a fake broker-only keychain; unreadable makes a service's reads fail as a reinstalled broker's would.
type secrets struct {
	items      map[string][]byte
	unreadable map[string]bool
	putErr     error
}

func (s *secrets) Secret(service, account string) ([]byte, error) {
	if s.unreadable[service] {
		return nil, errLeaky
	}
	b, ok := s.items[service+"/"+account]
	if !ok {
		return nil, ErrNotFound
	}
	return slices.Clone(b), nil
}

func (s *secrets) PutSecret(service, account string, data []byte) error {
	if s.putErr != nil {
		return s.putErr
	}
	s.items[service+"/"+account] = slices.Clone(data)
	return nil
}

func (s *secrets) DeleteSecret(service, account string) error {
	delete(s.items, service+"/"+account)
	return nil
}

type keys struct {
	account string
	key     []byte
	err     error
}

func (k keys) AppAccount(app string) (string, error) {
	if app != "agent-pm-bot" || k.err != nil {
		return "", errors.Join(ErrNotFound, k.err)
	}
	return k.account, nil
}

func (k keys) AppKey(app, account string) ([]byte, error) {
	if account != k.account {
		return nil, ErrNotFound
	}
	return slices.Clone(k.key), nil
}

// api is a fake GitHub: installations, issued tokens and revocations.
type api struct {
	mu        sync.Mutex
	installs  []string
	issued    int
	revoked   []string
	issueFail map[int]int // nth issue (1-based) → status
	revokeErr func(token string) int
	down      bool
}

func (a *api) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/app/installations":
			var items []string
			for i, login := range a.installs {
				items = append(items, fmt.Sprintf(`{"id":%d,"account":{"login":%q}}`, i+1, login))
			}
			_, _ = w.Write([]byte("[" + strings.Join(items, ",") + "]"))
		case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/app/installations/"):
			a.issued++
			if status := a.issueFail[a.issued]; status != 0 {
				w.WriteHeader(status)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/app/installations/"), "/access_tokens")
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprintf(w, `{"token":"ghs_%s_%d","expires_at":"2026-10-17T13:00:00Z"}`, id, a.issued)
		case r.Method == http.MethodDelete && r.URL.Path == "/installation/token":
			token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if a.revokeErr != nil {
				if status := a.revokeErr(token); status != 0 {
					w.WriteHeader(status)
					return
				}
			}
			a.revoked = append(a.revoked, token)
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL)
		}
	}
}

type rig struct {
	g    GitHub
	keys *secrets
	api  *api
	exec *fakeExec
	srv  *httptest.Server
}

func newRig(t *testing.T, installs ...string) *rig {
	t.Helper()
	r := &rig{keys: &secrets{items: map[string][]byte{}, unreadable: map[string]bool{}}, api: &api{installs: installs}, exec: &fakeExec{}}
	r.srv = httptest.NewServer(r.api.handler(t))
	t.Cleanup(r.srv.Close)
	r.g = GitHub{
		Keychain: r.keys,
		Apps:     keys{account: "5352992", key: testAppKey(t)},
		API:      github.Client{Base: r.srv.URL, HTTP: r.srv.Client(), Now: func() time.Time { return clock }},
		Lock:     FileLock(filepath.Join(t.TempDir(), "tokens.lock")),
	}
	return r
}

func (r *rig) record(t *testing.T) (rec tokenRecord, ok bool) {
	t.Helper()
	data, found := r.keys.items[tokensService+"/"+ctr]
	if !found {
		return rec, false
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	return rec, true
}

func (r *rig) backlog(t *testing.T) []pending {
	t.Helper()
	var list []pending
	if data, ok := r.keys.items[revocationsService+"/"+revocationsAccount]; ok {
		if err := json.Unmarshal(data, &list); err != nil {
			t.Fatal(err)
		}
	}
	return list
}

func wantCode(t *testing.T, err error, code protocol.Code) {
	t.Helper()
	var e *protocol.Error
	if !errors.As(err, &e) || e.Code != code {
		t.Fatalf("want %s, got %v", code, err)
	}
	if strings.Contains(err.Error(), "ghs_") {
		t.Fatalf("error leaks a token: %v", err)
	}
}

func TestReadyGitHub(t *testing.T) {
	r := newRig(t)
	if err := r.g.Ready("agent-pm-bot"); err != nil {
		t.Fatal(err)
	}
	wantCode(t, r.g.Ready("agent-pm-engineer"), protocol.ManifestRejected)
	r.g.Apps = keys{err: ErrNotFound}
	err := r.g.Ready("agent-pm-bot")
	wantCode(t, err, protocol.LoginRequired)
	if !strings.Contains(err.Error(), "agent-pm-broker login github agent-pm-bot") {
		t.Fatalf("message %v", err)
	}
	r.g.Apps = keys{account: "not-an-id", key: testAppKey(t)}
	wantCode(t, r.g.Ready("agent-pm-bot"), protocol.LoginRequired)
}

func TestIssueWritesFiles(t *testing.T) {
	r := newRig(t, "ophis", "Other-Org")
	got, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	if err != nil {
		t.Fatal(err)
	}
	want := []protocol.Expiry{{Account: "Other-Org", ExpiresAt: "2026-10-17T13:00:00Z"}, {Account: "ophis", ExpiresAt: "2026-10-17T13:00:00Z"}}
	if !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	if len(r.exec.argv) != 2 {
		t.Fatalf("execs %v", r.exec.argv)
	}
	for i, account := range []string{"ophis", "Other-Org"} {
		argv := r.exec.argv[i]
		if argv[0] != "/bin/sh" || argv[1] != "-c" || argv[2] != writeScript || argv[3] != "sh" || argv[4] != account || len(argv) != 5 {
			t.Fatalf("argv %q", argv)
		}
		if string(r.exec.stdin[i]) != fmt.Sprintf("ghs_%d_%d", i+1, i+1) {
			t.Fatalf("stdin %q", r.exec.stdin[i])
		}
		if strings.Contains(strings.Join(argv, " "), "ghs_") {
			t.Fatal("token in argv")
		}
	}
	for _, part := range []string{"umask 0377", "mkdir -p -m 0700 " + docker.SecretsDir + "/github", `mv -f `} {
		if !strings.Contains(writeScript, part) {
			t.Errorf("script lacks %q", part)
		}
	}
	rec, ok := r.record(t)
	if !ok || rec.App != "agent-pm-bot" || len(rec.Tokens) != 2 || rec.Tokens[0] != (tokenEntry{"ophis", 1, "ghs_1_1", "2026-10-17T13:00:00Z"}) {
		t.Fatalf("record %v", rec)
	}
}

func TestIssueRecordsBeforeWrite(t *testing.T) {
	r := newRig(t, "ophis")
	var recorded bool
	exec := func(argv []string, stdin []byte) error {
		_, recorded = r.record(t)
		return nil
	}
	if _, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, exec); err != nil || !recorded {
		t.Fatalf("recorded %v err %v", recorded, err)
	}
}

func TestIssueNoInstallations(t *testing.T) {
	r := newRig(t)
	got, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	if err != nil || len(got) != 0 || len(r.exec.argv) != 0 {
		t.Fatalf("%v %v %v", got, err, r.exec.argv)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("empty record kept")
	}
}

func TestIssueFailureRevokes(t *testing.T) {
	for name, tc := range map[string]struct {
		setup func(r *rig)
		code  protocol.Code
	}{
		"second issue": {func(r *rig) { r.api.issueFail = map[int]int{2: 500} }, protocol.GitHubFailed},
		"write":        {func(r *rig) { r.exec.err = errors.New("exec") }, ""},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t, "ophis", "other")
			tc.setup(r)
			_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
			if err == nil {
				t.Fatal("no error")
			}
			if tc.code != "" {
				wantCode(t, err, tc.code)
			}
			if _, ok := r.record(t); ok {
				t.Fatal("record kept")
			}
			if len(r.backlog(t)) != 0 || !slices.Contains(r.api.revoked, "ghs_1_1") {
				t.Fatalf("revoked %v backlog %v", r.api.revoked, r.backlog(t))
			}
		})
	}
}

func TestIssueRollbackOffline(t *testing.T) {
	r := newRig(t, "ophis", "other")
	r.api.issueFail = map[int]int{2: 500}
	r.api.revokeErr = func(string) int { return 503 }
	_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	if b := r.backlog(t); len(b) != 1 || b[0] != (pending{ctr, "ophis", "ghs_1_1", "2026-10-17T13:00:00Z"}) {
		t.Fatalf("backlog %v", b)
	}
}

// lockFails makes every token lock after the first ok ones fail.
func (r *rig) lockFails(ok int) {
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n > ok {
			return nil, errors.New("flock: input/output error")
		}
		return lock()
	}
}

func TestLockFailureRevokesTheNewToken(t *testing.T) {
	for name, run := range map[string]func(r *rig) error{
		"issue": func(r *rig) error {
			r.lockFails(0)
			_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
			return err
		},
		"refresh": func(r *rig) error {
			issued(t, r)
			r.lockFails(1)
			_, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run)
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t, "ophis")
			err := run(r)
			wantCode(t, err, protocol.KeychainFailed)
			if !strings.Contains(err.Error(), "token lock") {
				t.Fatalf("unclear error: %v", err)
			}
			if !slices.Contains(r.api.revoked, "ghs_1_"+strconv.Itoa(r.api.issued)) || len(r.exec.argv) != 0 {
				t.Fatalf("revoked %v writes %v", r.api.revoked, r.exec.argv)
			}
		})
	}
}

func TestLockFailureQueuesAnUnrevokedToken(t *testing.T) {
	r := newRig(t, "ophis")
	r.lockFails(0)
	r.api.revokeErr = func(string) int { return 503 }
	_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
	if b := r.backlog(t); len(b) != 1 || b[0].Token != "ghs_1_1" {
		t.Fatalf("backlog %v", b)
	}
}

func TestRevokeLockFails(t *testing.T) {
	r := newRig(t, "ophis", "other")
	issued(t, r)
	r.lockFails(0)
	if err := r.g.Revoke(context.Background(), ctr); err == nil || !strings.Contains(err.Error(), "token lock") {
		t.Fatalf("unclear error: %v", err)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("stale record of revoked tokens kept")
	}
}

func TestRevokeLockFailsKeepsATokenRecordedMeanwhile(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.lockFails(0)
	r.api.revokeErr = func(string) int { // a refresh records its new token while close revokes without the lock
		rec, _ := r.record(t)
		if !slices.ContainsFunc(rec.Tokens, func(e tokenEntry) bool { return e.Token == "ghs_late" }) {
			rec.Tokens = append(rec.Tokens, tokenEntry{"ophis", 1, "ghs_late", "2026-10-17T13:00:00Z"})
			data, _ := json.Marshal(rec)
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return 0
	}
	_ = r.g.Revoke(context.Background(), ctr)
	rec, ok := r.record(t)
	if !ok || len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_late" {
		t.Fatalf("record %v %v", ok, rec.Tokens)
	}
}

func TestIssueLockFailureLeavesNoRecord(t *testing.T) {
	r := newRig(t, "ophis", "other")
	r.lockFails(1)
	_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
	if _, ok := r.record(t); ok || !slices.Contains(r.api.revoked, "ghs_1_1") {
		t.Fatalf("record kept or revoked %v", r.api.revoked)
	}
}

func TestRevokeReadsTheRecordUnderTheLock(t *testing.T) {
	r := newRig(t, "ophis")
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n == 1 { // a refresh's record write lands just before close takes the lock
			data, _ := json.Marshal(tokenRecord{"agent-pm-bot", []tokenEntry{{"ophis", 1, "ghs_late", "2026-10-17T13:00:00Z"}}})
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return lock()
	}
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if _, ok := r.record(t); ok || !slices.Contains(r.api.revoked, "ghs_late") {
		t.Fatalf("record kept or revoked %v", r.api.revoked)
	}
}

func TestIssueKeyProblems(t *testing.T) {
	r := newRig(t, "ophis")
	r.g.Apps = keys{account: "5352992", key: []byte("garbage")}
	_, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	r.g.Apps = keys{err: ErrNotFound}
	_, err = r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.LoginRequired)
	if r.api.issued != 0 {
		t.Fatal("issued without a key")
	}
}

func issued(t *testing.T, r *rig) {
	t.Helper()
	if _, err := r.g.Issue(context.Background(), "agent-pm-bot", ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	r.exec.argv, r.exec.stdin = nil, nil
}

func TestRefresh(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	got, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	if err != nil || !slices.Equal(got, []protocol.Expiry{{Account: "ophis", ExpiresAt: "2026-10-17T13:00:00Z"}}) {
		t.Fatalf("%v %v", got, err)
	}
	if len(r.exec.argv) != 1 || string(r.exec.stdin[0]) != "ghs_1_2" {
		t.Fatalf("writes %q", r.exec.stdin)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
		t.Fatalf("revoked %v (only the record's old value)", r.api.revoked)
	}
	rec, _ := r.record(t)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_2" {
		t.Fatalf("record %v", rec)
	}
}

func TestRefreshNoRecord(t *testing.T) {
	r := newRig(t, "ophis")
	_, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.RefreshFailed)
	if r.api.issued != 0 {
		t.Fatal("issued")
	}
}

func TestRefreshRecordGone(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.api.revokeErr = func(token string) int {
		return 0
	}
	// a close takes the record away while refresh asks GitHub
	srv := r.g.API
	r.g.API.HTTP = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		if req.Method == http.MethodPost {
			delete(r.keys.items, tokensService+"/"+ctr)
		}
		return srv.HTTP.Transport.RoundTrip(req)
	})}
	_, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.RefreshFailed)
	if len(r.exec.argv) != 0 || !slices.Contains(r.api.revoked, "ghs_1_2") {
		t.Fatalf("writes %v revoked %v", r.exec.argv, r.api.revoked)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record recreated")
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestRefreshWriteFails(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.exec.err = errors.New("gone")
	if _, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run); err == nil {
		t.Fatal("no error")
	}
	rec, _ := r.record(t)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_1" || !slices.Equal(r.api.revoked, []string{"ghs_1_2"}) {
		t.Fatalf("record %v revoked %v", rec, r.api.revoked)
	}
}

func TestRefreshOldRevokeFails(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.api.revokeErr = func(token string) int {
		if token == "ghs_1_1" {
			return 502
		}
		return 0
	}
	if _, err := r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	rec, _ := r.record(t)
	if b := r.backlog(t); len(b) != 1 || b[0].Token != "ghs_1_1" || len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_2" {
		t.Fatalf("backlog %v record %v", b, rec)
	}
}

func TestRevoke(t *testing.T) {
	r := newRig(t, "ophis", "other")
	issued(t, r)
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if len(r.exec.argv) != 0 {
		t.Fatal("revoke ran in the container")
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	if _, ok := r.record(t); ok || len(r.backlog(t)) != 0 {
		t.Fatal("left state")
	}
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatalf("absent record: %v", err)
	}
}

func TestRevokeTakesATokenRecordedMeanwhile(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n == 2 { // a refresh records its new token between close's lock sections
			rec, _ := r.record(t)
			rec.Tokens = append(rec.Tokens, tokenEntry{"ophis", 1, "ghs_late", rec.Tokens[0].ExpiresAt})
			data, _ := json.Marshal(rec)
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return lock()
	}
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(r.api.revoked, "ghs_late") || len(r.backlog(t)) != 0 {
		t.Fatalf("revoked %v backlog %v", r.api.revoked, r.backlog(t))
	}
}

func TestRevokeOffline(t *testing.T) {
	r := newRig(t, "ophis", "other")
	issued(t, r)
	r.api.revokeErr = func(string) int { return 0 }
	r.srv.Close()
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if b := r.backlog(t); len(b) != 2 || b[0].Container != ctr {
		t.Fatalf("backlog %v", b)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record kept")
	}
}

func TestRetryRevocations(t *testing.T) {
	r := newRig(t)
	list := []pending{
		{"c1", "a", "ghs_expired", "2026-10-17T11:59:59Z"},
		{"c1", "b", "ghs_live", "2026-10-17T12:30:00Z"},
		{"c2", "c", "ghs_down", "2026-10-17T12:30:00Z"},
		{"c2", "d", "ghs_after", "2026-10-17T12:30:00Z"},
	}
	data, _ := json.Marshal(list)
	r.keys.items[revocationsService+"/"+revocationsAccount] = data
	var calls []string
	r.g.API.HTTP = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		token := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
		calls = append(calls, token)
		if token == "ghs_down" {
			return nil, errors.New("dial tcp: timeout")
		}
		return &http.Response{StatusCode: 204, Body: http.NoBody}, nil
	})}
	got, err := r.g.Retry(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(calls, []string{"ghs_live", "ghs_down"}) {
		t.Fatalf("calls %v: expired skipped, stop at the first transport error", calls)
	}
	want := []protocol.Pending{{Container: "c2", Account: "c", ExpiresAt: "2026-10-17T12:30:00Z"}, {Container: "c2", Account: "d", ExpiresAt: "2026-10-17T12:30:00Z"}}
	if !slices.Equal(got, want) {
		t.Fatalf("pending %v", got)
	}
	if b := r.backlog(t); len(b) != 2 {
		t.Fatalf("backlog %v", b)
	}
}

func TestUnreadableBacklog(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.keys.unreadable[revocationsService] = true
	_, err := r.g.Retry(context.Background())
	wantCode(t, err, protocol.KeychainFailed)
	if err := r.g.Revoke(context.Background(), ctr); err != nil {
		t.Fatalf("readable record revoked directly: %v", err)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	issued(t, r)
	r.api.revokeErr = func(string) int { return 500 }
	wantCode(t, r.g.Revoke(context.Background(), ctr), protocol.KeychainFailed)
}

func TestUnreadableRecord(t *testing.T) {
	r := newRig(t, "ophis")
	issued(t, r)
	r.keys.unreadable[tokensService] = true
	err := r.g.Revoke(context.Background(), ctr)
	wantCode(t, err, protocol.KeychainFailed)
	if !strings.Contains(err.Error(), "expire within an hour") {
		t.Fatalf("message %v", err)
	}
	if _, ok := r.keys.items[tokensService+"/"+ctr]; ok {
		t.Fatal("record not deleted")
	}
	r.keys.items[tokensService+"/"+ctr] = []byte("{}")
	_, err = r.g.Refresh(context.Background(), "agent-pm-bot", ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
}

func TestFileLock(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sub", "tokens.lock")
	unlock, err := FileLock(path)()
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
	unlock()
	unlock, err = FileLock(path)()
	if err != nil {
		t.Fatal(err)
	}
	unlock()
}
