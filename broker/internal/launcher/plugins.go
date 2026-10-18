package launcher

import (
	"encoding/json"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

var pluginNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// pluginMarkers are the runtime files Claude Code writes into an installed plugin's directory; they are not its content.
var pluginMarkers = []string{".in_use", ".orphaned_at"}

const pluginRoot = "/opt/agent-pm/plugins/"

// stagePlugins snapshots each named Claude Code plugin the account installed at user scope into dir/<manifest name>,
// returning the manifest names sorted. A name is name or name@marketplace, as in installed_plugins.json.
func (h Host) stagePlugins(names []string, dir string) ([]string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, protocol.New(protocol.BuildFailed, "Cannot stage plugins.")
	}
	if len(names) == 0 {
		return nil, nil
	}
	base := filepath.Join(h.Home, ".claude/plugins")
	recordPath := filepath.Join(base, "installed_plugins.json")
	var record struct {
		Plugins map[string][]struct {
			Scope       string `json:"scope"`
			InstallPath string `json:"installPath"`
		} `json:"plugins"`
	}
	data, err := os.ReadFile(recordPath)
	if err != nil || json.Unmarshal(data, &record) != nil {
		return nil, protocol.New(protocol.BuildFailed, "Cannot read the Claude Code install record "+recordPath+".")
	}
	cache, cacheErr := filepath.EvalSymlinks(filepath.Join(base, "cache"))
	var staged []string
	for _, name := range names {
		paths := map[string]string{}
		for id, entries := range record.Plugins {
			plugin, _, _ := strings.Cut(id, "@")
			if id != name && (strings.Contains(name, "@") || plugin != name) {
				continue
			}
			for _, entry := range entries {
				if entry.Scope == "user" && entry.InstallPath != "" {
					paths[id] = entry.InstallPath
					break
				}
			}
		}
		ids := slices.Sorted(maps.Keys(paths))
		switch {
		case len(ids) == 0:
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+" is not installed: no user-scope entry in "+recordPath+".")
		case len(ids) > 1:
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+" matches "+strings.Join(ids, ", ")+"; name one as "+name+"@<marketplace>.")
		}
		installed := paths[ids[0]]
		src, err := filepath.EvalSymlinks(installed)
		info, statErr := os.Stat(src)
		if cacheErr != nil || err != nil || statErr != nil || !info.IsDir() || !strings.HasPrefix(src, cache+string(filepath.Separator)) {
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": install path "+installed+" is not an installed plugin directory.")
		}
		if link, err := leavingLink(src); err != nil || link != "" {
			if err != nil {
				return nil, protocol.New(protocol.BuildFailed, "Cannot read plugin "+name+".")
			}
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": link "+link+" leaves the plugin directory.")
		}
		var manifest struct {
			Name string `json:"name"`
		}
		data, err := os.ReadFile(filepath.Join(src, ".claude-plugin/plugin.json"))
		if err != nil || json.Unmarshal(data, &manifest) != nil || !pluginNamePattern.MatchString(manifest.Name) {
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": no valid .claude-plugin/plugin.json name.")
		}
		if slices.Contains(staged, manifest.Name) {
			return nil, protocol.New(protocol.BuildFailed, "Two plugins named "+manifest.Name+".")
		}
		if err := snapshotTree(src, filepath.Join(dir, manifest.Name), pluginMarkers...); err != nil {
			return nil, protocol.New(protocol.BuildFailed, "Cannot read plugin "+name+".")
		}
		staged = append(staged, manifest.Name)
	}
	slices.Sort(staged)
	return staged, nil
}

// leavingLink is the root-relative path of the first symlink under root whose target is absolute or outside root, else "".
func leavingLink(root string) (string, error) {
	var leaving string
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.Type()&fs.ModeSymlink == 0 {
			return err
		}
		target, err := os.Readlink(path)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		resolved, resolveErr := filepath.EvalSymlinks(path)
		if inside, err := filepath.Rel(root, resolved); filepath.IsAbs(target) || resolveErr != nil || err != nil ||
			inside == ".." || strings.HasPrefix(inside, ".."+string(filepath.Separator)) {
			leaving = rel
			return fs.SkipAll
		}
		return nil
	})
	return leaving, err
}

// pluginDirs is the image's CLAUDE_CODE_PLUGIN_DIRS for the staged plugin names.
func pluginDirs(names []string) string {
	dirs := make([]string, len(names))
	for i, name := range names {
		dirs[i] = pluginRoot + name
	}
	return strings.Join(dirs, ":")
}
