package broker

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestFileKeychain(t *testing.T) {
	if os.Getenv("CI") == "" {
		t.Skip("CI only: security create-keychain adds the throwaway keychain to the user's search list")
	}
	path := filepath.Join(t.TempDir(), "throwaway.keychain-db")
	security := func(args ...string) string {
		t.Helper()
		// A keychain prompt on a headless runner fails the test instead of hanging the job.
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		out, err := exec.CommandContext(ctx, "/usr/bin/security", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("security %q: %v %s", args, err, out)
		}
		return string(out)
	}
	security("create-keychain", "-p", "throwaway", path)
	t.Cleanup(func() { _ = exec.Command("/usr/bin/security", "delete-keychain", path).Run() })
	security("unlock-keychain", "-p", "throwaway", path)
	k := fileKeychain(path)
	if err := k.Exists(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing item: Exists %v", err)
	}
	if _, err := k.Get(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing item: Get %v", err)
	}
	// The second Put replaces the item through token_put's delete-then-add.
	for _, token := range []string{"first-token", "second-token"} {
		if err := k.Put([]byte(token)); err != nil {
			t.Fatal(err)
		}
		if err := k.Exists(); err != nil {
			t.Fatal(err)
		}
		if got, err := k.Get(); err != nil || string(got) != token {
			t.Fatalf("Get %q %v, want %q", got, err, token)
		}
	}
	security("delete-generic-password", "-s", "agent-pm-claude", "-a", "setup-token", path)
	if err := k.Exists(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted item: Exists %v", err)
	}
	if _, err := k.Get(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted item: Get %v", err)
	}
	const app, key = "agent-pm-test-app", "dGhyb3dhd2F5LWtleQ=="
	if _, err := k.AppAccount(app); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing app item: %v", err)
	}
	// -A lets the test binary read the seed without a prompt, as the user allows once.
	security("add-generic-password", "-A", "-s", app, "-a", "4242", "-w", key, path)
	created := regexp.MustCompile(`"cdat"<timedate>=\S+`).FindString(security("find-generic-password", "-s", app, path))
	self, err := os.Executable()
	if err != nil || created == "" {
		t.Fatalf("%q %v", created, err)
	}
	for range 2 {
		var out bytes.Buffer
		if err := LoginGitHub(app, k, &out); err != nil || strings.Contains(out.String(), key) {
			t.Fatalf("%v %q", err, out.String())
		}
		if got, err := k.AppKey(app, "4242"); err != nil || string(got) != key {
			t.Fatalf("AppKey %q %v", got, err)
		}
		// Changed in place: the item keeps its creation date, and no entry trusts every application.
		if now := security("find-generic-password", "-s", app, path); !strings.Contains(now, created) {
			t.Fatalf("item re-created: %s, was %s", now, created)
		}
		if acl := security("dump-keychain", "-a", path); strings.Contains(acl, "applications: <null>") || !strings.Contains(acl, self) {
			t.Fatalf("ACL not broker-only:\n%s", acl)
		}
	}
	security("add-generic-password", "-A", "-s", app, "-a", "4343", "-w", key, path)
	if _, err := k.AppAccount(app); !errors.Is(err, ErrAmbiguous) {
		t.Fatalf("two app items: %v", err)
	}
	security("delete-keychain", path)
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("keychain kept: %v", err)
	}
}
