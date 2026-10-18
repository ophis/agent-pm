package launcher

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const home = "/Users/example"

// mountHost sees every directory as present but those in missing.
func mountHost(missing ...string) Host {
	return Host{
		Home:   home,
		IsDir:  func(p string) bool { return !slices.Contains(missing, p) },
		Safe:   func(string) error { return nil },
		GitHub: &fakeTokens{},
	}
}

func baseManifest() Manifest {
	return Manifest{Name: "w", Generation: strings.Repeat("0", 32), CWD: home + "/src/project", Workspace: "agent-pm-ws-w",
		ClaudeDir: home + "/.agent-pm/docker-state/w/claude", HookPath: "/usr/local/bin/agent-pm-session", HasHook: true}
}

func TestMountRules(t *testing.T) {
	ok := mountHost()
	for _, cwd := range []string{home + "/src/project", home + "/.agent-pm/adhoc/x/agent", "/home/alice/src/p", "/tmp/demo", "/opt/other/p", "/var/tmp/p", "/usr2", "/homework"} {
		m := baseManifest()
		m.CWD = cwd
		if err := ok.checkMounts(m); err != nil {
			t.Errorf("%s: %v", cwd, err)
		}
	}
	type tc struct {
		name   string
		host   Host
		edit   func(*Manifest)
		reason string
	}
	cwd := func(p string) func(*Manifest) { return func(m *Manifest) { m.CWD = p } }
	cases := []tc{
		{"missing cwd", mountHost(home + "/src/project"), nil, "existing host directory"},
		{"root", ok, cwd("/"), "contains"},
		{"home", ok, cwd(home), "claude_dir"},
		{"Users", ok, cwd("/Users"), "claude_dir"},
		{"state dir", ok, cwd(home + "/.agent-pm/docker-state/w"), "claude_dir"},
		{"claude dir", ok, cwd(home + "/.agent-pm/docker-state/w/claude"), "claude_dir"},
		{"inside claude dir", ok, cwd(home + "/.agent-pm/docker-state/w/claude/x"), "claude_dir"},
		{"claude dir wrong", ok, func(m *Manifest) { m.ClaudeDir = home + "/.agent-pm/docker-state/other/claude" }, "claude_dir"},
		{"claude dir trailing slash", ok, func(m *Manifest) { m.ClaudeDir += "/" }, "claude_dir"},
		{"hook in cwd", ok, func(m *Manifest) { m.HookPath = home + "/src/project/bin/hook" }, "hook_path"},
		{"hook is cwd", ok, func(m *Manifest) { m.HookPath = home + "/src/project" }, "hook_path"},
		{"cwd inside hook", ok, func(m *Manifest) { m.CWD, m.HookPath = home+"/src/project", home+"/src" }, "hook_path"},
		{"hook in claude dir", ok, func(m *Manifest) { m.HookPath = m.ClaudeDir + "/hook" }, "hook_path"},
		{"hook in secrets", ok, func(m *Manifest) { m.HookPath = "/run/agent-pm/secrets/claude" }, "hook_path"},
		{"hook dotdot", ok, func(m *Manifest) { m.HookPath = "/tmp/../" + home + "/src/project/h" }, "hook_path"},
		{"hook dot", ok, func(m *Manifest) { m.HookPath = "/usr/./bin/h" }, "hook_path"},
		{"hook empty component", ok, func(m *Manifest) { m.HookPath = "/usr//bin/h" }, "hook_path"},
	}
	for _, p := range []string{"/usr", "/etc", "/opt", "/run", "/home", "/tmp", "/proc", "/sys", "/dev"} {
		cases = append(cases, tc{"equal " + p, ok, cwd(p), "contains"})
	}
	for _, p := range []string{"/usr/lib/x", "/etc/x", "/opt/agent-pm", "/opt/agent-pm/lib", "/run/agent-pm", "/proc/1", "/sys/x", "/dev/x",
		"/bin", "/sbin/x", "/lib/x", "/lib32", "/lib64/x", "/libx32", "/var/run/agent-pm", "/var/lock", "/home/agent", "/home/agent/.npm-global"} {
		cases = append(cases, tc{"inside " + p, ok, cwd(p), "inside"})
	}
	unsafe := mountHost()
	unsafe.Safe = func(string) error { return errors.New("symlink component") }
	cases = append(cases, tc{"claude dir symlink", unsafe, nil, "claude_dir"})
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := baseManifest()
			if c.edit != nil {
				c.edit(&m)
			}
			err := c.host.checkMounts(m)
			var e *protocol.Error
			if !errors.As(err, &e) || e.Code != protocol.MountRejected || !strings.Contains(e.Message, c.reason) {
				t.Fatalf("%v", err)
			}
		})
	}
	noHook := baseManifest()
	noHook.HasHook, noHook.HookPath = false, ""
	if err := ok.checkMounts(noHook); err != nil {
		t.Fatal(err)
	}
}

func TestMountCSVQuoting(t *testing.T) {
	if got := bindMount("/a,b", `/c"d`); got != `type=bind,"source=/a,b","target=/c""d"` {
		t.Fatal(got)
	}
	if got := volumeMount("agent-pm-ws-w", "/a,b", true); got != `type=volume,source=agent-pm-ws-w,"target=/a,b",readonly` {
		t.Fatal(got)
	}
	if got := volumeMount("v", "/w", false); got != `type=volume,source=v,target=/w` {
		t.Fatal(got)
	}
}
