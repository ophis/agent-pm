package launcher

import (
	"errors"
	"os"
	"path/filepath"
	"regexp"
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
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + `","cwd":"/w","workspace":"agent-pm-crew-w","claude_dir":"/c","github":[],"container_config":"","labels":{}`
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

func TestManifestGitHubAndContainerConfig(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-role-run.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest(data)
	want := GitHubToken{Repo: "ophis/agent-pm", App: "agent-pm-engineer", AppID: 5352992, Permissions: map[string]string{"contents": "write", "pull_requests": "write"}}
	if err != nil || len(m.GitHub) != 1 || m.GitHub[0].Repo != want.Repo || m.GitHub[0].App != want.App || m.GitHub[0].AppID != want.AppID ||
		len(m.GitHub[0].Permissions) != 2 || m.GitHub[0].Permissions["contents"] != "write" || m.ContainerConfig != "[delegate]\nusers = [\"octocat\"]\ntrusted_dirs = []\n" {
		t.Fatalf("%+v %v", m, err)
	}
	with := func(old, new string) error {
		if !strings.Contains(string(data), old) {
			t.Fatalf("fixture has no %s", old)
		}
		_, err := ParseManifest([]byte(strings.Replace(string(data), old, new, 1)))
		return err
	}
	if _, err := ParseManifest(regexp.MustCompile(`(?s)"permissions": \{.*?\}`).ReplaceAll(data, []byte(`"permissions": {}`))); code(t, err) != protocol.ManifestRejected {
		t.Error("empty permissions accepted")
	}
	for name, edit := range map[string][2]string{
		"repo dot":         {`"ophis/agent-pm"`, `"ophis/.."`},
		"repo host":        {`"ophis/agent-pm"`, `"github.com/ophis/agent-pm"`},
		"app id zero":      {`5352992`, `0`},
		"app id float":     {`5352992`, `5352992.5`},
		"app id string":    {`5352992`, `"5352992"`},
		"app id too large": {`5352992`, `9007199254740992`},
		"app upper":        {`"agent-pm-engineer"`, `"Agent"`},
		"permission name":  {`"pull_requests"`, `"Pull"`},
		"null item":        {`"github": [`, `"github": [null, `},
		"not array":        {`"github": [`, `"github": {"a": [`},
		"config not text":  {`"container_config": "`, `"container_config": 1, "x": "`},
		"config escape":    {`[delegate]`, `\u001b[delegate]`},
	} {
		if code(t, with(edit[0], edit[1])) != protocol.ManifestRejected {
			t.Errorf("%s accepted", name)
		}
	}
	if err := with(`trusted_dirs = []`, `\ttrusted_dirs = []`); err != nil {
		t.Errorf("tab rejected: %v", err)
	}
	if err := with(`"[delegate]`, `"`+strings.Repeat("x", maxContainerConfig)); code(t, err) != protocol.ManifestRejected {
		t.Error("oversized container_config accepted")
	}
}
