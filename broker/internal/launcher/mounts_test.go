package launcher

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const home = "/Users/example"

func mountHost(roots []string, real map[string]string) Host {
	return Host{
		Home:  home,
		Roots: func() ([]string, error) { return roots, nil },
		RealPath: func(p string) (string, error) {
			if r, ok := real[p]; ok {
				return r, nil
			}
			return p, nil
		},
		Safe: func(string) error { return nil },
		Same: func(a, b string) bool { return a == b },
	}
}

func baseManifest() Manifest {
	return Manifest{Name: "w", Generation: strings.Repeat("0", 32), CWD: home + "/src/project",
		ClaudeDir: home + "/.agent-pm/docker-state/w/claude", HookPath: "/usr/local/bin/agent-pm-session", HasHook: true}
}

func TestMountRules(t *testing.T) {
	roots := []string{home + "/src"}
	ok := mountHost(roots, nil)
	if real, err := ok.checkMounts(baseManifest()); err != nil || real != home+"/src/project" {
		t.Fatalf("%q %v", real, err)
	}
	type tc struct {
		name   string
		host   Host
		edit   func(*Manifest)
		reason string
	}
	cases := []tc{
		{"no roots", mountHost(nil, nil), nil, "mount_roots"},
		{"roots unreadable", Host{Home: home, Roots: func() ([]string, error) { return nil, errors.New("bad toml") }}, nil, "mount_roots"},
		{"home root", mountHost([]string{home + "/src", home}, nil), nil, "mount root"},
		{"ancestor root", mountHost([]string{"/Users"}, nil), nil, "mount root"},
		{"root resolves to home", mountHost([]string{"/link"}, map[string]string{"/link": home}), nil, "mount root"},
		{"outside roots", ok, func(m *Manifest) { m.CWD = "/tmp/elsewhere" }, "outside"},
		{"symlink to secret", mountHost(roots, map[string]string{home + "/src/project": home + "/.ssh"}), nil, ""},
		{"contains denied", mountHost([]string{"/data"}, map[string]string{home + "/src/project": "/data/keys", home + "/.ssh": "/data/keys/ssh"}), nil, "denied"},
		{"claude dir wrong", ok, func(m *Manifest) { m.ClaudeDir = home + "/.agent-pm/docker-state/other/claude" }, "claude_dir"},
		{"claude dir trailing slash", ok, func(m *Manifest) { m.ClaudeDir += "/" }, "claude_dir"},
		{"hook in cwd", ok, func(m *Manifest) { m.HookPath = home + "/src/project/bin/hook" }, "hook_path"},
		{"hook is cwd", ok, func(m *Manifest) { m.HookPath = home + "/src/project" }, "hook_path"},
		{"hook in claude dir", ok, func(m *Manifest) { m.HookPath = m.ClaudeDir + "/hook" }, "hook_path"},
		{"hook in secrets", ok, func(m *Manifest) { m.HookPath = "/run/agent-pm/secrets/claude" }, "hook_path"},
		{"hook dotdot", ok, func(m *Manifest) { m.HookPath = "/tmp/../" + home + "/src/project/h" }, "hook_path"},
		{"hook dot", ok, func(m *Manifest) { m.HookPath = "/usr/./bin/h" }, "hook_path"},
		{"hook empty component", ok, func(m *Manifest) { m.HookPath = "/usr//bin/h" }, "hook_path"},
	}
	unsafe := mountHost(roots, nil)
	unsafe.Safe = func(string) error { return errors.New("symlink component") }
	cases = append(cases, tc{"claude dir symlink", unsafe, nil, "claude_dir"})
	for _, name := range []string{".agent-pm/x", ".ssh", "Library", ".codex", ".claude", ".config", ".docker", ".aws", ".gnupg", ".kube", ".npmrc"} {
		cwd := home + "/" + name
		cases = append(cases, tc{"deny " + name, mountHost([]string{home + "/src", cwd}, nil), func(m *Manifest) { m.CWD = cwd }, "denied"})
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := baseManifest()
			if c.edit != nil {
				c.edit(&m)
			}
			_, err := c.host.checkMounts(m)
			var e *protocol.Error
			if !errors.As(err, &e) || e.Code != protocol.MountRejected || !strings.Contains(e.Message, c.reason) {
				t.Fatalf("%v", err)
			}
		})
	}
	noHook := baseManifest()
	noHook.HasHook, noHook.HookPath = false, ""
	if _, err := ok.checkMounts(noHook); err != nil {
		t.Fatal(err)
	}
}

func TestIdentityCaseVariant(t *testing.T) {
	dir := t.TempDir()
	upper := filepath.Join(dir, "Secret")
	if err := os.Mkdir(upper, 0o755); err != nil {
		t.Fatal(err)
	}
	lower := filepath.Join(dir, "secret")
	if _, err := os.Stat(lower); err != nil {
		t.Skip("case-sensitive filesystem")
	}
	inside := filepath.Join(lower, "x")
	if err := os.Mkdir(inside, 0o755); err != nil {
		t.Fatal(err)
	}
	h := Host{Same: realSame}
	if !h.within(inside, upper) || !h.within(lower, upper) || h.within(dir, upper) {
		t.Fatal("identity comparison missed a case variant")
	}
	if !realSame("/nonexistent/AbC", "/nonexistent/abc") || realSame(upper, "/nonexistent") {
		t.Fatal("absent paths")
	}
}
