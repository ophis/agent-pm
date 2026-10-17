package launcher

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestLoadRoots(t *testing.T) {
	home := t.TempDir()
	write := func(text string) {
		t.Helper()
		putFile(t, filepath.Join(home, ".agent-pm/config.local.toml"), text)
	}
	if roots, err := LoadRoots(home); err != nil || len(roots) != 0 {
		t.Fatalf("missing file: %q %v", roots, err)
	}
	write("[session]\nx = 1\n")
	if roots, err := LoadRoots(home); err != nil || len(roots) != 0 {
		t.Fatalf("missing key: %q %v", roots, err)
	}
	write("[session.hosts]\ndefault = \"docker\"\n\n[session.hosts.docker]\nmount_roots = [\"~/src\", \"~\", \"/abs/work\"]\n")
	roots, err := LoadRoots(home)
	if err != nil || !slices.Equal(roots, []string{filepath.Join(home, "src"), home, "/abs/work"}) {
		t.Fatalf("%q %v", roots, err)
	}
	write("[session]\nhosts.docker.mount_roots = [\"/a\"]\n")
	if roots, err := LoadRoots(home); err != nil || !slices.Equal(roots, []string{"/a"}) {
		t.Fatalf("dotted: %q %v", roots, err)
	}
	for _, bad := range []string{
		"[session.hosts.docker]\nmount_roots = \"/a\"\n",
		"[session.hosts.docker]\nmount_roots = [1]\n",
		"[session.hosts.docker]\nmount_roots = [\"relative\"]\n",
		"[session.hosts.docker]\nmount_roots = [\"~user/x\"]\n",
		"[session.hosts\n",
	} {
		write(bad)
		if _, err := LoadRoots(home); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
	if err := os.Chmod(filepath.Join(home, ".agent-pm/config.local.toml"), 0); err == nil && os.Getuid() != 0 {
		if _, err := LoadRoots(home); err == nil {
			t.Error("unreadable config accepted")
		}
	}
}
