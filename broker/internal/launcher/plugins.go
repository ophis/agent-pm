package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

var pluginSourcePattern = regexp.MustCompile(`^([A-Za-z0-9][A-Za-z0-9._-]{0,63})@([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99})$`)

const pluginRoot = "/opt/agent-pm/plugins/"

type pluginSource struct {
	Name string `json:"name"`
	Repo string `json:"repo"`
}

// stagePlugins records repository sources, never host client installation state.
func stagePlugins(names []string, dir string) ([]string, error) {
	sources := []pluginSource{}
	for _, value := range names {
		name, repo := "autopilot", "ophis/claude-autopilot"
		if value != "autopilot" {
			match := pluginSourcePattern.FindStringSubmatch(value)
			if match == nil {
				return nil, protocol.New(protocol.BuildFailed, "Plugin "+value+": use autopilot or name@owner/repo.")
			}
			name, repo = match[1], match[2]
		}
		for _, source := range sources {
			if source.Name == name {
				return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": duplicate plugin name.")
			}
		}
		sources = append(sources, pluginSource{Name: name, Repo: repo})
	}
	slices.SortFunc(sources, func(a, b pluginSource) int { return strings.Compare(a.Name, b.Name) })
	data, err := json.Marshal(sources)
	if err == nil {
		err = os.WriteFile(filepath.Join(dir, "plugin-sources.json"), data, 0o644)
	}
	if err != nil {
		return nil, protocol.New(protocol.BuildFailed, "Cannot stage plugin sources.")
	}
	staged := make([]string, len(sources))
	for i, source := range sources {
		staged[i] = source.Name
	}
	return staged, nil
}

func pluginDirs(names []string) string {
	dirs := make([]string, len(names))
	for i, name := range names {
		dirs[i] = pluginRoot + name
	}
	return strings.Join(dirs, ":")
}
