package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestBuildStagesPluginSourcesWithoutHostRecord(t *testing.T) {
	e := newBuildEnv(t)
	// A directory here makes any attempt to read a host install record fail.
	if err := os.MkdirAll(filepath.Join(e.home, ".claude/plugins/installed_plugins.json"), 0o700); err != nil {
		t.Fatal(err)
	}
	inner := e.fake.respond
	seen := false
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" {
			if !slices.Contains(args, "--no-cache") {
				t.Fatal("client/plugin refresh used Docker cache")
			}
			if slices.Contains(args, "--build-arg") {
				seen = true
				data, err := os.ReadFile(filepath.Join(args[len(args)-1], "plugin-sources.json"))
				if err != nil {
					t.Fatal(err)
				}
				var sources []map[string]string
				if err := json.Unmarshal(data, &sources); err != nil || len(sources) != 2 || sources[0]["repo"] != "ophis/claude-autopilot" || sources[1]["repo"] != "owner/market" {
					t.Fatalf("sources %s: %v", data, err)
				}
				if !slices.Contains(args, "AGENT_PM_PLUGIN_DIRS=/opt/agent-pm/plugins/autopilot:/opt/agent-pm/plugins/beta") {
					t.Fatalf("plugin dirs absent: %q", args)
				}
			}
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, []string{"beta@owner/market", "autopilot"}, -1); err != nil {
		t.Fatal(err)
	}
	if !seen {
		t.Fatal("package build absent")
	}
}

func TestPluginSourcesChangeTheKeyButHostPluginsDoNot(t *testing.T) {
	e := newBuildEnv(t)
	build := func(names ...string) BuildResult {
		t.Helper()
		result, err := e.host().Build(context.Background(), e.pkg, names, -1)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	none, first := build(), build("autopilot")
	if none.Image == first.Image {
		t.Fatal("plugins absent from key")
	}
	putFile(t, filepath.Join(e.home, ".claude/plugins/installed_plugins.json"), "invalid JSON")
	if again := build("autopilot"); again.Rebuilt || again.Image != first.Image {
		t.Fatal("host plugins affected image")
	}
	if next := build("autopilot@owner/other"); next.Image == first.Image {
		t.Fatal("repo absent from key")
	}
}

func TestPluginSourceValidation(t *testing.T) {
	for _, names := range [][]string{{"unknown"}, {"autopilot@market"}, {"x@../repo"}, {"x@owner/.."}, {"x@owner/repo#ref"}, {"autopilot", "autopilot@owner/repo"}} {
		t.Run(strings.Join(names, ","), func(t *testing.T) {
			e := newBuildEnv(t)
			_, err := e.host().Build(context.Background(), e.pkg, names, -1)
			if err == nil || !strings.Contains(err.Error(), "Plugin") || len(e.fake.calls) != 0 {
				t.Fatalf("invalid source accepted: %v; %q", err, e.fake.calls)
			}
		})
	}
}

func TestEmptyPluginSources(t *testing.T) {
	e := newBuildEnv(t)
	tmp := t.TempDir()
	_, names, err := e.host().stage(e.pkg, nil, tmp)
	if err != nil || len(names) != 0 {
		t.Fatalf("%q %v", names, err)
	}
	data, err := os.ReadFile(filepath.Join(tmp, "package/plugin-sources.json"))
	if err != nil || string(data) != "[]" {
		t.Fatalf("%s %v", data, err)
	}
}
