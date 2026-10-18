package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// install writes a plugin whose manifest names it name under the account's plugin cache and records it as id at user scope.
func (e *buildEnv) install(t *testing.T, id, name string) string {
	t.Helper()
	dir := filepath.Join(e.home, ".claude/plugins/cache/m", strings.ReplaceAll(id, "@", "-"), "1.0.0")
	putFile(t, filepath.Join(dir, ".claude-plugin/plugin.json"), `{"name":"`+name+`","version":"1.0.0"}`)
	putFile(t, filepath.Join(dir, "skills/go/SKILL.md"), "go "+id)
	e.record(t, id, map[string]any{"scope": "user", "installPath": dir})
	return dir
}

func (e *buildEnv) record(t *testing.T, id string, entries ...map[string]any) {
	t.Helper()
	path := filepath.Join(e.home, ".claude/plugins/installed_plugins.json")
	var rec struct {
		Plugins map[string][]map[string]any `json:"plugins"`
	}
	if data, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(data, &rec); err != nil {
			t.Fatal(err)
		}
	}
	if rec.Plugins == nil {
		rec.Plugins = map[string][]map[string]any{}
	}
	rec.Plugins[id] = entries
	data, _ := json.Marshal(map[string]any{"version": 2, "plugins": rec.Plugins})
	putFile(t, path, string(data))
}

// staged records the second stage's build context listing and its AGENT_PM_PLUGIN_DIRS build arg.
func (e *buildEnv) staged() (*[]string, *string) {
	files, dirs := &[]string{}, new(string)
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" && slices.Contains(args, "--build-arg") {
			for i, a := range args {
				if v, ok := strings.CutPrefix(a, "AGENT_PM_PLUGIN_DIRS="); ok && args[i-1] == "--build-arg" {
					*dirs = v
				}
			}
			root := filepath.Join(args[len(args)-1], "plugins")
			*files = nil
			_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
				if err == nil && !entry.IsDir() {
					rel, _ := filepath.Rel(root, path)
					*files = append(*files, rel)
				}
				return err
			})
		}
		return inner(args)
	}
	return files, dirs
}

func TestBuildStagesPlugins(t *testing.T) {
	e := newBuildEnv(t)
	dir := e.install(t, "autopilot@claude-autopilot", "autopilot")
	e.install(t, "beta@m", "beta")
	putFile(t, filepath.Join(dir, ".in_use/123"), "pid")
	putFile(t, filepath.Join(dir, ".orphaned_at"), "1")
	files, dirs := e.staged()
	if _, err := e.host().Build(context.Background(), e.pkg, []string{"beta", "autopilot@claude-autopilot"}, -1); err != nil {
		t.Fatal(err)
	}
	want := []string{"autopilot/.claude-plugin/plugin.json", "autopilot/skills/go/SKILL.md", "beta/.claude-plugin/plugin.json", "beta/skills/go/SKILL.md"}
	if !slices.Equal(*files, want) {
		t.Fatalf("staged %q", *files)
	}
	if *dirs != "/opt/agent-pm/plugins/autopilot:/opt/agent-pm/plugins/beta" {
		t.Fatalf("plugin dirs %q", *dirs)
	}
}

func TestBuildWithoutPluginsStagesNone(t *testing.T) {
	e := newBuildEnv(t)
	files, dirs := e.staged()
	*dirs = "unset"
	if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
		t.Fatal(err)
	}
	if len(*files) != 0 || *dirs != "" {
		t.Fatalf("%q %q", *files, *dirs)
	}
}

func TestPluginsChangeTheKey(t *testing.T) {
	e := newBuildEnv(t)
	dir := e.install(t, "autopilot@m", "autopilot")
	build := func(plugins ...string) BuildResult {
		t.Helper()
		r, err := e.host().Build(context.Background(), e.pkg, plugins, -1)
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	none, first := build(), build("autopilot")
	if none.Image == first.Image {
		t.Fatal("whitelist did not change the key")
	}
	putFile(t, filepath.Join(dir, ".in_use/456"), "pid")
	putFile(t, filepath.Join(dir, ".orphaned_at"), "2")
	if again := build("autopilot"); again.Rebuilt || again.Image != first.Image {
		t.Fatalf("runtime markers changed the key: %+v", again)
	}
	putFile(t, filepath.Join(dir, "skills/go/SKILL.md"), "upgraded")
	if upgraded := build("autopilot"); !upgraded.Rebuilt || upgraded.Image == first.Image {
		t.Fatalf("plugin content did not change the key: %+v", upgraded)
	}
}

func TestBuildSnapshotsALinkedInstallPath(t *testing.T) {
	e := newBuildEnv(t)
	dir := e.install(t, "autopilot@m", "autopilot")
	link := filepath.Join(e.home, ".claude/plugins/cache/m/current")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	e.record(t, "autopilot@m", map[string]any{"scope": "user", "installPath": link})
	files, _ := e.staged()
	if _, err := e.host().Build(context.Background(), e.pkg, []string{"autopilot"}, -1); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(*files, "autopilot/skills/go/SKILL.md") {
		t.Fatalf("staged %q", *files)
	}
}

func TestPluginFailures(t *testing.T) {
	record := func(e *buildEnv) string { return filepath.Join(e.home, ".claude/plugins/installed_plugins.json") }
	for name, c := range map[string]struct {
		plugins []string
		setup   func(*testing.T, *buildEnv)
		want    func(*buildEnv) string
	}{
		"not installed": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) { e.install(t, "other@m", "other") },
			func(e *buildEnv) string {
				return "Plugin autopilot is not installed: no user-scope entry in " + record(e) + "."
			}},
		"project scope only": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			dir := e.install(t, "autopilot@m", "autopilot")
			e.record(t, "autopilot@m", map[string]any{"scope": "project", "installPath": dir, "projectPath": "/p"})
		}, func(e *buildEnv) string {
			return "Plugin autopilot is not installed: no user-scope entry in " + record(e) + "."
		}},
		"other marketplace named": {[]string{"autopilot@x"}, func(t *testing.T, e *buildEnv) { e.install(t, "autopilot@m", "autopilot") },
			func(e *buildEnv) string {
				return "Plugin autopilot@x is not installed: no user-scope entry in " + record(e) + "."
			}},
		"ambiguous": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			e.install(t, "autopilot@b", "autopilot")
			e.install(t, "autopilot@a", "autopilot")
		}, func(*buildEnv) string {
			return "Plugin autopilot matches autopilot@a, autopilot@b; name one as autopilot@<marketplace>."
		}},
		"no record": {[]string{"autopilot"}, func(*testing.T, *buildEnv) {},
			func(e *buildEnv) string { return "Cannot read the Claude Code install record " + record(e) + "." }},
		"malformed record": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) { putFile(t, record(e), `{"plugins":[]}`) },
			func(e *buildEnv) string { return "Cannot read the Claude Code install record " + record(e) + "." }},
		"outside the cache": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			dir := filepath.Join(e.home, "src/autopilot")
			putFile(t, filepath.Join(dir, ".claude-plugin/plugin.json"), `{"name":"autopilot"}`)
			e.record(t, "autopilot@m", map[string]any{"scope": "user", "installPath": dir})
		}, func(e *buildEnv) string {
			return "Plugin autopilot: install path " + filepath.Join(e.home, "src/autopilot") + " is not an installed plugin directory."
		}},
		"missing install path": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			e.install(t, "other@m", "other")
			e.record(t, "autopilot@m", map[string]any{"scope": "user", "installPath": filepath.Join(e.home, ".claude/plugins/cache/m/gone")})
		}, func(e *buildEnv) string {
			return "Plugin autopilot: install path " + filepath.Join(e.home, ".claude/plugins/cache/m/gone") + " is not an installed plugin directory."
		}},
		"no manifest": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			os.Remove(filepath.Join(e.install(t, "autopilot@m", "autopilot"), ".claude-plugin/plugin.json"))
		}, func(*buildEnv) string { return "Plugin autopilot: no valid .claude-plugin/plugin.json name." }},
		"bad manifest name": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) { e.install(t, "autopilot@m", "../x") },
			func(*buildEnv) string { return "Plugin autopilot: no valid .claude-plugin/plugin.json name." }},
		"two of one name": {[]string{"autopilot", "copy"}, func(t *testing.T, e *buildEnv) {
			e.install(t, "autopilot@m", "autopilot")
			e.install(t, "copy@m", "autopilot")
		}, func(*buildEnv) string { return "Two plugins named autopilot." }},
		"link leaves the plugin": {[]string{"autopilot"}, func(t *testing.T, e *buildEnv) {
			dir := e.install(t, "autopilot@m", "autopilot")
			if err := os.Symlink(e.home, filepath.Join(dir, "skills/home")); err != nil {
				t.Fatal(err)
			}
		}, func(*buildEnv) string { return "Plugin autopilot: link skills/home leaves the plugin directory." }},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			c.setup(t, e)
			_, err := e.host().Build(context.Background(), e.pkg, c.plugins, -1)
			if got := protoErr(t, err); got.Code != protocol.BuildFailed || got.Message != c.want(e) {
				t.Fatalf("%v", err)
			}
			if len(e.fake.calls) != 0 {
				t.Fatalf("docker called: %q", e.fake.verbs())
			}
		})
	}
}

func TestWorkerImageHoldsItsPlugins(t *testing.T) {
	data, err := assets.ReadFile("assets/Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"COPY plugins /opt/agent-pm/plugins\n",
		"&& chmod -R a+rX /opt/agent-pm/plugins \\\n    && chown -R root:root /opt/agent-pm && chmod -R go-w /opt/agent-pm",
		"ARG AGENT_PM_PLUGIN_DIRS\nENV CLAUDE_CODE_PLUGIN_DIRS=${AGENT_PM_PLUGIN_DIRS}\n",
	} {
		if !strings.Contains(string(data), want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
}
