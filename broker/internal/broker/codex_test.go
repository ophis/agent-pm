package broker

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ophis/agent-pm/broker/internal/codex"
)

type fakeCodexNative struct {
	verifyErr, runErr error
	data              []byte
	home              string
	locked            bool
	runs              int
	onRun             func(string)
}

func (n *fakeCodexNative) Verify() (codex.Installation, error) {
	return codex.Installation{Version: "0.162.1", Path: "/fixed/codex", SHA256: strings.Repeat("a", 64), AllowedVersions: []string{}}, n.verifyErr
}

func (n *fakeCodexNative) Lock(ctx context.Context) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	n.locked = true
	return func() { n.locked = false }, nil
}

func (n *fakeCodexNative) Run(ctx context.Context, home string, args ...string) error {
	n.home, n.runs = home, n.runs+1
	if !n.locked {
		return errors.New("not locked")
	}
	if !slices.Equal(args, []string{"-c", `cli_auth_credentials_store="file"`, "-c", `forced_login_method="chatgpt"`, "login"}) {
		return errors.New("wrong arguments")
	}
	if n.onRun != nil {
		n.onRun(home)
	}
	if n.runErr != nil {
		return n.runErr
	}
	return os.WriteFile(filepath.Join(home, "auth.json"), n.data, 0o600)
}

type fakeCodexKeychain struct {
	fakeApps
	native         *fakeCodexNative
	data           []byte
	missing        bool
	writeErr       error
	writeBreaksACL bool
	events         []string
}

func (k *fakeCodexKeychain) AppAccess(service, account string) (AppAccess, error) {
	if !k.native.locked {
		return AppAccess{}, errors.New("unlocked metadata read")
	}
	if service != codexService || account != codexAccount {
		return AppAccess{}, errors.New("wrong item")
	}
	if k.missing {
		return AppAccess{}, ErrNotFound
	}
	return k.fakeApps.AppAccess(service, account)
}

func (k *fakeCodexKeychain) Secret(service, account string) ([]byte, error) {
	if !k.native.locked {
		return nil, errors.New("unlocked read")
	}
	if service != codexService || account != codexAccount {
		return nil, errors.New("wrong item")
	}
	k.events = append(k.events, "read")
	if k.missing {
		return nil, ErrNotFound
	}
	if a, err := k.AppAccess(service, account); err != nil || onlyBroker(a) != nil {
		return nil, errors.New("not trusted")
	}
	return slices.Clone(k.data), k.getErr
}

func (k *fakeCodexKeychain) CreateCodex() error {
	k.events = append(k.events, "create")
	if !k.missing {
		return errors.New("already exists")
	}
	k.missing, k.access = false, added()
	return nil
}

func (k *fakeCodexKeychain) WriteCodex(data []byte) error {
	if !k.native.locked {
		return errors.New("unlocked write")
	}
	if err := onlyBroker(*k.access); err != nil {
		return errors.New("write before trust")
	}
	k.events = append(k.events, "write")
	if k.writeErr != nil {
		return k.writeErr
	}
	k.data = slices.Clone(data)
	if k.writeBreaksACL {
		k.access = added()
	}
	return nil
}

var codexNow = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)

func codexAuth(exp int64) []byte {
	payload, _ := json.Marshal(map[string]int64{"exp": exp})
	token := "eyJhbGciOiJSUzI1NiJ9." + base64.RawURLEncoding.EncodeToString(payload) + ".signature"
	data, _ := json.Marshal(map[string]any{"auth_mode": "chatgpt", "OPENAI_API_KEY": nil, "tokens": map[string]string{"access_token": token, "refresh_token": "fake-refresh-secret", "id_token": "fake-id-secret", "account_id": "fake-account-secret"}, "last_refresh": codexNow.Format(time.RFC3339)})
	return data
}

func codexFixture(t *testing.T) (Codex, *fakeCodexNative, *fakeCodexKeychain) {
	t.Helper()
	n := &fakeCodexNative{data: codexAuth(codexNow.Add(time.Hour).Unix())}
	k := &fakeCodexKeychain{native: n, missing: true}
	return Codex{native: n, keychain: k, tempRoot: t.TempDir(), now: func() time.Time { return codexNow }, uid: func() int { return 501 }, password: func() ([]byte, error) { return []byte(keychainPass), nil }}, n, k
}

func TestCodexLoginIsolation(t *testing.T) {
	c, n, k := codexFixture(t)
	n.onRun = func(home string) {
		info, err := os.Stat(home)
		if err != nil || info.Mode().Perm() != 0o700 {
			t.Fatal("nonprivate home")
		}
		if filepath.Dir(home) != c.tempRoot {
			t.Fatal("wrong temp root")
		}
	}
	var out bytes.Buffer
	if err := c.Login(context.Background(), &out); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(n.home); !os.IsNotExist(err) {
		t.Fatal("temporary credentials survived")
	}
	if n.locked {
		t.Fatal("lock leaked")
	}
	if !slices.Equal(k.steps, []string{"partition agent-pm-codex auth", "apps agent-pm-codex auth"}) {
		t.Fatalf("ACL order %v", k.steps)
	}
	if !bytes.Equal(k.data, n.data) {
		t.Fatal("wrong stored auth")
	}
	if strings.Contains(out.String(), "secret") || strings.Contains(out.String(), "eyJ") {
		t.Fatal("secret output")
	}
}

func TestCodexLoginFailureCleanup(t *testing.T) {
	for _, runErr := range []error{errors.New("child fake-refresh-secret"), context.Canceled} {
		c, n, k := codexFixture(t)
		n.runErr = runErr
		var out bytes.Buffer
		err := c.Login(context.Background(), &out)
		if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") {
			t.Fatalf("unsafe error %v", err)
		}
		if _, err := os.Stat(n.home); !os.IsNotExist(err) {
			t.Fatal("temporary home survived")
		}
		if !k.missing || n.locked {
			t.Fatal("failure persisted login or lock")
		}
	}
}

func TestCodexLoginRejectsBeforeRun(t *testing.T) {
	for _, kind := range []string{"root", "pin", "cancel"} {
		c, n, _ := codexFixture(t)
		ctx := context.Background()
		switch kind {
		case "root":
			c.uid = func() int { return 0 }
		case "pin":
			n.verifyErr = errors.New("bad hash")
		case "cancel":
			var cancel context.CancelFunc
			ctx, cancel = context.WithCancel(ctx)
			cancel()
		}
		if c.Login(ctx, &bytes.Buffer{}) == nil || n.runs != 0 {
			t.Fatal(kind)
		}
	}
}

func TestCodexAuthValidation(t *testing.T) {
	for name, data := range map[string][]byte{
		"empty": nil, "malformed": []byte("fake-refresh-secret"), "expired": codexAuth(codexNow.Unix()),
		"api key":         []byte(`{"OPENAI_API_KEY":"fake-api-secret"}`),
		"missing refresh": []byte(`{"tokens":{"access_token":"a.b.c","id_token":"id"}}`),
		"oversize":        bytes.Repeat([]byte("a"), maxCodexAuth+1),
	} {
		t.Run(name, func(t *testing.T) {
			c, n, k := codexFixture(t)
			n.data = data
			err := c.Login(context.Background(), &bytes.Buffer{})
			if err == nil || strings.Contains(err.Error(), "fake-") {
				t.Fatalf("unsafe auth result %v", err)
			}
			if !k.missing {
				t.Fatal("invalid auth saved")
			}
		})
	}
}

func TestCodexReloginAndTrustFailure(t *testing.T) {
	for _, kind := range []string{"relogin", "partition", "readback", "write", "postwrite"} {
		t.Run(kind, func(t *testing.T) {
			c, n, k := codexFixture(t)
			old := codexAuth(codexNow.Add(2 * time.Hour).Unix())
			k.missing, k.data, k.access = false, slices.Clone(old), reinstalled()
			switch kind {
			case "partition":
				k.partitionErr = errors.New("fake-refresh-secret")
			case "readback":
				k.frozen = true
			case "write":
				k.writeErr = errors.New("fake-refresh-secret")
			case "postwrite":
				k.writeBreaksACL = true
			}
			err := c.Login(context.Background(), &bytes.Buffer{})
			if kind == "relogin" {
				if err != nil || !bytes.Equal(k.data, n.data) {
					t.Fatal(err)
				}
				return
			}
			if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") {
				t.Fatalf("unsafe error %v", err)
			}
			if kind != "postwrite" && !bytes.Equal(k.data, old) {
				t.Fatal("old login overwritten")
			}
		})
	}
}

func TestCodexStatusSanitized(t *testing.T) {
	for _, kind := range []string{"ready", "missing", "expired", "invalid", "pin", "trust", "read"} {
		t.Run(kind, func(t *testing.T) {
			c, n, k := codexFixture(t)
			a := brokerOnly()
			k.access, k.missing, k.data = &a, false, n.data
			switch kind {
			case "missing":
				k.missing = true
			case "expired":
				k.data = codexAuth(codexNow.Add(-time.Hour).Unix())
			case "invalid":
				k.data = []byte("fake-refresh-secret")
			case "pin":
				n.verifyErr = errors.New("bad pin")
			case "trust":
				k.access = added()
			case "read":
				k.getErr = errors.New("fake-refresh-secret")
			}
			status := c.Status(context.Background())
			if status.LoggedIn != (kind == "ready") {
				t.Fatalf("wrong status %#v", status)
			}
			data, _ := json.Marshal(status)
			for _, secret := range []string{"fake-", "eyJ", "access_token\"", "refresh_token"} {
				if bytes.Contains(data, []byte(secret)) {
					t.Fatal("secret status")
				}
			}
			if kind == "ready" && status.ExpiresAt != codexNow.Add(time.Hour).Format(time.RFC3339) {
				t.Fatal("wrong expiry")
			}
		})
	}
}

type serializedCodexNative struct {
	*fakeCodexNative
	gate chan struct{}
}

func (n *serializedCodexNative) Lock(ctx context.Context) (func(), error) {
	select {
	case n.gate <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	n.locked = true
	return func() { n.locked = false; <-n.gate }, nil
}

func TestCodexConcurrentLoginRereads(t *testing.T) {
	c, n, k := codexFixture(t)
	c.native = &serializedCodexNative{fakeCodexNative: n, gate: make(chan struct{}, 1)}
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for range 2 {
		wg.Go(func() { errs <- c.Login(context.Background(), &bytes.Buffer{}) })
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if n.runs != 2 || !slices.Equal(k.events, []string{"create", "write", "read", "read", "write", "read"}) {
		t.Fatalf("missing locked reread %v", k.events)
	}
}

func TestCodexRejectsCredentialSymlink(t *testing.T) {
	c, n, k := codexFixture(t)
	target := filepath.Join(t.TempDir(), "other")
	n.onRun = func(home string) {
		if err := os.Symlink(target, filepath.Join(home, "auth.json")); err != nil {
			t.Fatal(err)
		}
	}
	if c.Login(context.Background(), &bytes.Buffer{}) == nil || !k.missing {
		t.Fatal("accepted credential symlink")
	}
}
