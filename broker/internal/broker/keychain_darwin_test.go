package broker

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestFileKeychain(t *testing.T) {
	if os.Getenv("CI") == "" {
		t.Skip("CI only: security create-keychain adds the throwaway keychain to the user's search list")
	}
	path := filepath.Join(t.TempDir(), "throwaway.keychain-db")
	security := func(args ...string) {
		t.Helper()
		if out, err := exec.Command("/usr/bin/security", args...).CombinedOutput(); err != nil {
			t.Fatalf("security %q: %v %s", args, err, out)
		}
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
	security("delete-keychain", path)
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("keychain kept: %v", err)
	}
}
