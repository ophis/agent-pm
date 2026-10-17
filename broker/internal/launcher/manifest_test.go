package launcher

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/fixtures"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

func code(t *testing.T, err error) protocol.Code {
	t.Helper()
	var e *protocol.Error
	if err == nil {
		return ""
	}
	if !errors.As(err, &e) {
		t.Fatalf("not a protocol error: %v", err)
	}
	return e.Code
}

func TestManifestFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "manifest") {
		t.Run(c.File, func(t *testing.T) {
			m, err := ParseManifest(c.Data)
			if c.Valid {
				if err != nil || m.Name == "" || m.Labels == nil {
					t.Fatalf("%+v %v", m, err)
				}
				return
			}
			if code(t, err) != protocol.ManifestRejected {
				t.Fatalf("accepted: %+v %v", m, err)
			}
		})
	}
}

func TestManifestDuplicatesTrailingUTF8(t *testing.T) {
	valid, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest(valid)
	if err != nil || !m.HasHook || m.HookPath != "/usr/local/bin/agent-pm-session" || m.Labels["team"] != "crew" {
		t.Fatalf("%+v %v", m, err)
	}
	base := `"generation":"0123456789abcdef0123456789abcdef","image":"agent-pm-worker:` +
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + `","cwd":"/w","workspace":"agent-pm-crew-w","claude_dir":"/c","github":null,"labels":{}`
	if _, err := ParseManifest([]byte(`{"name":"w",` + base + `}`)); err != nil {
		t.Fatal(err)
	}
	for name, doc := range map[string]string{
		"duplicate":       `{"name":"w","name":"v",` + base + `}`,
		"duplicate label": `{"name":"w",` + base[:len(base)-2] + `{"a":"1","a":"2"}}`,
		"trailing":        `{"name":"w",` + base + `} {}`,
		"invalid utf8":    `{"name":"w",` + base[:len(base)-len(`"labels":{}`)] + `"labels":{"a":"` + "\xff" + `"}}`,
		"empty":           ``,
		"null label":      `{"name":"w",` + base[:len(base)-2] + `{"a":null}}`,
		"none name":       `{"name":"none",` + base + `}`,
		"nan":             `{"name":"w",` + base + `,"x":NaN}`,
	} {
		if _, err := ParseManifest([]byte(doc)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestManifestCleansCWD(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest([]byte(strings.Replace(string(data), `src/project"`, `src//project/."`, 1)))
	if err != nil || m.CWD != "/Users/example/src/project" {
		t.Fatalf("%q %v", m.CWD, err)
	}
}

func TestManifestWorkspace(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	with := func(workspace string) (Manifest, error) {
		return ParseManifest([]byte(strings.Replace(string(data), `"agent-pm-crew-worker_1"`, workspace, 1)))
	}
	for workspace, kind := range map[string]string{`"agent-pm-crew-worker_1"`: "crew", `"agent-pm-run-TASK-633-0123abcd"`: "run", `"agent-pm-run-a.b_c-ffffffff"`: "run"} {
		m, err := with(workspace)
		if err != nil || m.Kind() != kind || `"`+m.Workspace+`"` != workspace {
			t.Fatalf("%s: %+v %v", workspace, m, err)
		}
	}
	for _, workspace := range []string{`"agent-pm-crew-other"`, `"agent-pm-crew-worker_1x"`, `"agent-pm-run-x-0123abc"`, `"agent-pm-run-x-0123ABCD"`,
		`"agent-pm-run--0123abcd"`, `"agent-pm-run-a/b-0123abcd"`, `"volume"`, `""`, `1`, `null`} {
		if _, err := with(workspace); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", workspace)
		}
	}
}

func TestManifestGitHub(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m, err := ParseManifest(data); err != nil || m.GitHubApp != "agent-pm-bot" {
		t.Fatalf("%+v %v", m, err)
	}
	valid, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m, err := ParseManifest(valid); err != nil || m.GitHubApp != "" {
		t.Fatalf("%+v %v", m, err)
	}
	for _, github := range []string{`[]`, `"agent-pm-bot"`, `{"app":null}`, `{"app":"a","app":"b"}`, `{"app":"-a"}`} {
		doc := strings.Replace(string(valid), `"github": null`, `"github": `+github, 1)
		if _, err := ParseManifest([]byte(doc)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", github)
		}
	}
}
