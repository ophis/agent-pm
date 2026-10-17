package launcher

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
)

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
