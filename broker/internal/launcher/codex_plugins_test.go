package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func codexPluginStage(t *testing.T, e *buildEnv) string {
	t.Helper()
	tmp := t.TempDir()
	if _, _, err := e.host().stage(e.pkg, []string{"autopilot"}, tmp); err != nil {
		t.Fatal(err)
	}
	return filepath.Join(tmp, "package", "codex-plugins", "inline", "autopilot", "local")
}

func TestCodexPluginSkillsAdapter(t *testing.T) {
	e := newBuildEnv(t)
	src := e.install(t, "autopilot@m", "autopilot")
	putFile(t, filepath.Join(src, ".claude-plugin/plugin.json"), `{"name":"autopilot","version":"1.0.0","description":"Build","skills":["./extra"],"hooks":{"x":1},"mcpServers":{"x":{}}}`)
	putFile(t, filepath.Join(src, "extra/build/SKILL.md"), "build")
	putFile(t, filepath.Join(src, "references/detail.md"), "detail")
	for _, rel := range []string{"hooks/hooks.json", ".mcp.json", ".app.json"} {
		putFile(t, filepath.Join(src, rel), `{}`)
	}
	dst := codexPluginStage(t, e)
	data, err := os.ReadFile(filepath.Join(dst, ".codex-plugin/plugin.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest map[string]any
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"name": "autopilot", "version": "1.0.0", "description": "Build", "skills": []any{"./skills", "./extra"}}
	if !reflect.DeepEqual(manifest, want) {
		t.Fatalf("native adapter: %s", data)
	}
	for _, rel := range []string{"skills/go/SKILL.md", "extra/build/SKILL.md", "references/detail.md"} {
		if _, err := os.Stat(filepath.Join(dst, rel)); err != nil {
			t.Fatal(err)
		}
	}
	for _, rel := range []string{"hooks/hooks.json", ".mcp.json", ".app.json"} {
		if _, err := os.Lstat(filepath.Join(dst, rel)); !os.IsNotExist(err) {
			t.Fatalf("Claude default configuration imported: %s: %v", rel, err)
		}
		if _, err := os.Stat(filepath.Join(dst, "../../../../plugins/autopilot", rel)); err != nil {
			t.Fatalf("Claude plugin changed: %v", err)
		}
	}
}

func TestCodexNativePluginManifestPreserved(t *testing.T) {
	e := newBuildEnv(t)
	src := e.install(t, "autopilot@m", "autopilot")
	manifest := `{"name":"autopilot","skills":"./skills","hooks":{"hooks":{}},"version":"2.0.0"}`
	putFile(t, filepath.Join(src, ".codex-plugin/plugin.json"), manifest)
	putFile(t, filepath.Join(src, "hooks/hooks.json"), `{}`)
	dst := codexPluginStage(t, e)
	data, err := os.ReadFile(filepath.Join(dst, ".codex-plugin/plugin.json"))
	if err != nil || string(data) != manifest {
		t.Fatalf("native metadata changed: %s: %v", data, err)
	}
	if _, err := os.Stat(filepath.Join(dst, "hooks/hooks.json")); err != nil {
		t.Fatal(err)
	}
}

func TestCodexPluginPathsAndMetadataFailClosed(t *testing.T) {
	for name, manifest := range map[string]string{
		"invalid JSON":    `[]`,
		"mismatched name": `{"name":"other"}`,
		"absolute skills": `{"name":"autopilot","skills":"/private"}`,
		"escaping skills": `{"name":"autopilot","skills":"./../private"}`,
		"invalid skills":  `{"name":"autopilot","skills":[1]}`,
		"missing skills":  `{"name":"autopilot","skills":"./missing"}`,
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			src := e.install(t, "autopilot@m", "autopilot")
			putFile(t, filepath.Join(src, ".codex-plugin/plugin.json"), manifest)
			if _, _, err := e.host().stage(e.pkg, []string{"autopilot"}, t.TempDir()); err == nil {
				t.Fatal("unsafe native plugin accepted")
			}
			if len(e.fake.calls) != 0 {
				t.Fatal("Docker called before plugin validation")
			}
		})
	}
}

func TestPluginChainedAndDanglingLinksFailClosed(t *testing.T) {
	for _, dangling := range []bool{false, true} {
		t.Run(map[bool]string{false: "chained", true: "dangling"}[dangling], func(t *testing.T) {
			e := newBuildEnv(t)
			src := e.install(t, "autopilot@m", "autopilot")
			putFile(t, filepath.Join(src, "a/keep"), "keep")
			if err := os.Symlink("..", filepath.Join(src, "a/b")); err != nil {
				t.Fatal(err)
			}
			target := "b/.."
			if dangling {
				target = "missing"
			}
			if err := os.Symlink(target, filepath.Join(src, "a/escape")); err != nil {
				t.Fatal(err)
			}
			if _, _, err := e.host().stage(e.pkg, []string{"autopilot"}, t.TempDir()); err == nil {
				t.Fatal("unsafe plugin link accepted")
			}
		})
	}
}

func TestEmptyCodexPluginCache(t *testing.T) {
	e := newBuildEnv(t)
	tmp := t.TempDir()
	if _, _, err := e.host().stage(e.pkg, nil, tmp); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(filepath.Join(tmp, "package/codex-plugins/inline"))
	if err != nil || len(entries) != 0 {
		t.Fatalf("empty whitelist: %v %v", entries, err)
	}
}
