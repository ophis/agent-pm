package launcher

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Fixed in the broker; configuration cannot change it.
var denied = []string{".agent-pm", ".codex", ".claude", ".ssh", ".config", ".docker", "Library", ".aws", ".gnupg", ".kube", ".npmrc"}

// within reports whether a or one of its ancestors is the same file as b.
func within(h Host, a, b string) bool {
	b = filepath.Clean(b)
	for x := filepath.Clean(a); ; x = filepath.Dir(x) {
		if h.Same(x, b) {
			return true
		}
		if x == "/" {
			return false
		}
	}
}

// realSame compares by file identity, so APFS case and normalization variants match.
func realSame(a, b string) bool {
	ia, ea := os.Stat(a)
	ib, eb := os.Stat(b)
	if ea == nil && eb == nil {
		return os.SameFile(ia, ib)
	}
	return ea != nil && eb != nil && strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

// realSafe requires an existing directory reached without any symlink component.
func realSafe(path string) error {
	p := "/"
	for _, part := range strings.Split(strings.Trim(path, "/"), "/") {
		p = filepath.Join(p, part)
		info, err := os.Lstat(p)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("%s is a symlink", p)
		}
	}
	if info, err := os.Stat(path); err != nil || !info.IsDir() {
		return fmt.Errorf("%s is not a directory", path)
	}
	return nil
}

// lexWithin is the container's view: mounts sit at literal paths there.
func lexWithin(a, b string) bool {
	return a == b || strings.HasPrefix(a, strings.TrimSuffix(b, "/")+"/")
}

// checkMounts applies the mount rules and returns cwd's real path, the bind source.
func checkMounts(h Host, m Manifest) (string, error) {
	reject := func(format string, args ...any) (string, error) {
		return "", protocol.Newf(protocol.MountRejected, format, args...)
	}
	roots, err := h.Roots()
	if err != nil {
		e := protocol.New(protocol.MountRejected, "Cannot read mount_roots from ~/.agent-pm/config.local.toml.")
		e.Cause = err.Error()
		return "", e
	}
	if len(roots) == 0 {
		return reject("No mount_roots configured: set [session.hosts.docker] mount_roots in ~/.agent-pm/config.local.toml.")
	}
	resolve := func(p string) string {
		if r, err := h.RealPath(p); err == nil {
			return r
		}
		return filepath.Clean(p)
	}
	var real []string
	for _, root := range roots {
		r := resolve(root)
		if within(h, h.Home, r) {
			return reject("mount root %s is the home directory or its ancestor.", root)
		}
		real = append(real, r)
	}
	cwd, err := h.RealPath(m.CWD)
	if err != nil {
		return reject("cwd %s cannot be resolved.", m.CWD)
	}
	if within(h, h.Home, cwd) {
		return reject("cwd %s is the home directory or its ancestor (denied).", m.CWD)
	}
	for _, name := range denied {
		d := resolve(filepath.Join(h.Home, name))
		if within(h, cwd, d) || within(h, d, cwd) {
			return reject("cwd %s overlaps denied path %s.", m.CWD, d)
		}
	}
	inside := false
	for _, r := range real {
		inside = inside || within(h, cwd, r)
	}
	if !inside {
		return reject("cwd %s is outside every mount root.", m.CWD)
	}
	if want := filepath.Join(h.Home, ".agent-pm/docker-state", m.Name, "claude"); m.ClaudeDir != want {
		return reject("claude_dir must be %s.", want)
	}
	if err := h.Safe(m.ClaudeDir); err != nil {
		return reject("claude_dir %s must be an existing directory without symlinks: %v.", m.ClaudeDir, err)
	}
	if m.HasHook {
		for _, part := range strings.Split(m.HookPath[1:], "/") {
			if part == "" || part == "." || part == ".." {
				return reject("hook_path %s must be a clean absolute path.", m.HookPath)
			}
		}
		for _, mount := range []string{m.CWD, cwd, m.ClaudeDir, docker.SecretsDir} {
			if lexWithin(m.HookPath, mount) {
				return reject("hook_path %s lies inside the mount %s.", m.HookPath, mount)
			}
		}
	}
	return cwd, nil
}
