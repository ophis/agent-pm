package launcher

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

func stageCodexPlugins(names []string, source, cache string) error {
	if err := os.MkdirAll(filepath.Join(cache, "inline"), 0o755); err != nil {
		return protocol.New(protocol.BuildFailed, "Cannot stage Codex plugins.")
	}
	for _, name := range names {
		src := filepath.Join(source, name)
		dst := filepath.Join(cache, "inline", name, "local")
		manifest, native, err := codexPluginManifest(src, name)
		if err != nil {
			return protocol.New(protocol.BuildFailed, "Plugin "+name+": "+err.Error())
		}
		var skip []string
		if !native {
			skip = []string{"hooks/hooks.json", ".mcp.json", ".app.json"}
		}
		if err := snapshotTree(src, dst, skip...); err != nil {
			return protocol.New(protocol.BuildFailed, "Cannot stage Codex plugin "+name+".")
		}
		if !native {
			path := filepath.Join(dst, ".codex-plugin")
			if err := os.MkdirAll(path, 0o755); err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(path, "plugin.json"), manifest, 0o644); err != nil {
				return err
			}
		}
	}
	return nil
}

func codexPluginManifest(root, name string) ([]byte, bool, error) {
	data, err := os.ReadFile(filepath.Join(root, ".codex-plugin/plugin.json"))
	native := err == nil
	if os.IsNotExist(err) {
		data, err = os.ReadFile(filepath.Join(root, ".claude-plugin/plugin.json"))
	}
	if err != nil {
		return nil, false, fmt.Errorf("cannot read plugin manifest")
	}
	var manifest map[string]json.RawMessage
	var declared string
	if json.Unmarshal(data, &manifest) != nil || json.Unmarshal(manifest["name"], &declared) != nil || declared != name {
		return nil, false, fmt.Errorf("invalid Codex plugin manifest name")
	}
	paths, err := pluginSkills(root, manifest["skills"], !native)
	if err != nil {
		return nil, false, err
	}
	if native {
		return data, true, nil
	}
	adapted := map[string]any{"name": name}
	for _, field := range []string{"description", "version"} {
		if value, ok := manifest[field]; ok {
			var text string
			if json.Unmarshal(value, &text) != nil {
				return nil, false, fmt.Errorf("invalid plugin %s", field)
			}
			adapted[field] = text
		}
	}
	if len(paths) != 0 {
		adapted["skills"] = paths
	}
	encoded, err := json.Marshal(adapted)
	return encoded, false, err
}

func pluginSkills(root string, raw json.RawMessage, addDefault bool) ([]string, error) {
	var paths []string
	if len(raw) != 0 {
		var single string
		if err := json.Unmarshal(raw, &single); err == nil {
			paths = []string{single}
		} else if err := json.Unmarshal(raw, &paths); err != nil {
			return nil, fmt.Errorf("invalid plugin skills: expected a path or paths")
		}
	}
	if addDefault {
		if info, err := os.Stat(filepath.Join(root, "skills")); err == nil && info.IsDir() {
			paths = append([]string{"./skills"}, paths...)
		}
	}
	var result []string
	for _, path := range paths {
		if path == "." {
			path = "./"
		}
		if !strings.HasPrefix(path, "./") || strings.ContainsAny(path, "\x00\\") || slices.Contains(strings.Split(path, "/"), "..") {
			return nil, fmt.Errorf("invalid plugin skills path: %q", path)
		}
		target, err := filepath.EvalSymlinks(filepath.Join(root, path))
		info, statErr := os.Stat(target)
		inside, relErr := filepath.Rel(root, target)
		if err != nil || statErr != nil || !info.IsDir() || relErr != nil || inside == ".." || strings.HasPrefix(inside, ".."+string(filepath.Separator)) {
			return nil, fmt.Errorf("invalid plugin skills directory: %q", path)
		}
		normalized := "./" + filepath.ToSlash(filepath.Clean(path))
		if !slices.Contains(result, normalized) {
			result = append(result, normalized)
		}
	}
	return result, nil
}
