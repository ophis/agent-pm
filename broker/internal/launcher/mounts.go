package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// Fixed in the broker; configuration cannot change it.
var denied = []string{".agent-pm", ".codex", ".claude", ".ssh", ".config", ".docker", "Library", ".aws", ".gnupg", ".kube", ".npmrc"}

// LoadRoots reads session.hosts.docker.mount_roots from <home>/.agent-pm/config.local.toml; absent means none.
func LoadRoots(home string) ([]string, error) {
	var config map[string]any
	if _, err := toml.DecodeFile(filepath.Join(home, ".agent-pm/config.local.toml"), &config); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var node any = config
	for _, key := range []string{"session", "hosts", "docker", "mount_roots"} {
		table, ok := node.(map[string]any)
		if !ok {
			return nil, errors.New("session.hosts.docker must be a table")
		}
		if node, ok = table[key]; !ok {
			return nil, nil
		}
	}
	list, ok := node.([]any)
	if !ok {
		return nil, errors.New("mount_roots must be an array of paths")
	}
	var roots []string
	for _, item := range list {
		root, ok := item.(string)
		switch {
		case !ok:
			return nil, errors.New("mount_roots must be an array of paths")
		case root == "~":
			root = home
		case strings.HasPrefix(root, "~/"):
			root = filepath.Join(home, root[2:])
		}
		if !strings.HasPrefix(root, "/") || hasControl(root) {
			return nil, fmt.Errorf("mount root %q must be absolute or start with ~/", root)
		}
		roots = append(roots, filepath.Clean(root))
	}
	return roots, nil
}

// within reports whether a or one of its ancestors is the same file as b.
func (h Host) within(a, b string) bool {
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

// checkMounts applies the mount rules and returns cwd's real path, the bind source.
func (h Host) checkMounts(m Manifest) (string, error) {
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
	var realRoots []string
	for _, root := range roots {
		r := resolve(root)
		if h.within(h.Home, r) {
			return reject("mount root %s is the home directory or its ancestor.", root)
		}
		realRoots = append(realRoots, r)
	}
	cwd, err := h.RealPath(m.CWD)
	if err != nil {
		return reject("cwd %s cannot be resolved.", m.CWD)
	}
	if h.within(h.Home, cwd) {
		return reject("cwd %s is the home directory or its ancestor (denied).", m.CWD)
	}
	for _, name := range denied {
		d := resolve(filepath.Join(h.Home, name))
		if h.within(cwd, d) || h.within(d, cwd) {
			return reject("cwd %s overlaps denied path %s.", m.CWD, d)
		}
	}
	inside := false
	for _, r := range realRoots {
		inside = inside || h.within(cwd, r)
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
			// The container's view: mounts sit at literal paths there.
			if m.HookPath == mount || strings.HasPrefix(m.HookPath, strings.TrimSuffix(mount, "/")+"/") {
				return reject("hook_path %s lies inside the mount %s.", m.HookPath, mount)
			}
		}
	}
	return cwd, nil
}

// checkNesting refuses a cwd strictly inside a running worker's bind source: that worker could swap
// a directory for a symlink between this check and Docker's mount.
func (h Host) checkNesting(ctx context.Context, cwd string) error {
	out, err := h.Docker.Run(ctx, []string{"ps", "--filter", "label=agent-pm.session", "--format", "{{.ID}}"}, nil)
	if err != nil {
		return failure(err, protocol.StartFailed, "Cannot list running workers.")
	}
	ids := strings.Fields(string(out))
	if len(ids) == 0 {
		return nil
	}
	out, err = h.Docker.Run(ctx, append([]string{"inspect", "--format", "{{json .Mounts}}"}, ids...), nil)
	if err != nil {
		return failure(err, protocol.StartFailed, "Cannot inspect running workers.")
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		var mounts []struct{ Type, Source string }
		if err := json.Unmarshal([]byte(line), &mounts); err != nil {
			return protocol.New(protocol.StartFailed, "Cannot read running workers' mounts.")
		}
		for _, m := range mounts {
			if m.Type == "bind" && m.Source != "" && h.within(cwd, m.Source) && !h.Same(cwd, m.Source) {
				return protocol.Newf(protocol.MountRejected, "cwd %s is inside running worker mount %s.", cwd, m.Source)
			}
		}
	}
	return nil
}
