package broker

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
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
	item := func() string {
		return security("find-generic-password", "-s", app, path) + security("dump-keychain", "-a", path)
	}
	before := item()
	// Changing an ACL always needs the user's authorization, which a headless runner cannot give:
	// with prompts off it must fail and leave the item exactly as it was. The success path is checked on a host.
	if err := setInteraction(false); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	err := LoginGitHub(app, k, &out)
	if err := setInteraction(true); err != nil {
		t.Fatal(err)
	}
	if err == nil || !strings.Contains(err.Error(), app+" unchanged") || strings.Contains(err.Error(), key) || out.Len() != 0 {
		t.Fatalf("%v %q", err, out.String())
	}
	if after := item(); after != before {
		t.Fatalf("item changed:\n%s\nwas:\n%s", after, before)
	}
	if got, err := k.AppKey(app, "4242"); err != nil || string(got) != key {
		t.Fatalf("AppKey %q %v", got, err)
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
