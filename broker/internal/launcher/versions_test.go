package launcher

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/codex"
)

type fakeVersions struct {
	allowed                           []string
	target, latest                    string
	verifyErr, registryErr, binaryErr error
	arch                              string
}

func (v *fakeVersions) Verify() (codex.Installation, error) {
	return codex.Installation{Version: "0.162.1", AllowedVersions: v.allowed}, v.verifyErr
}
func (v *fakeVersions) LatestAllowed(context.Context, []string) (string, error) {
	return v.target, v.registryErr
}
func (v *fakeVersions) Latest(context.Context) (string, error) { return v.latest, v.registryErr }
func (v *fakeVersions) Linux(_ context.Context, _ string, arch string) ([]byte, error) {
	v.arch = arch
	return []byte("fake-native"), v.binaryErr
}

func defaultVersions() *fakeVersions {
	return &fakeVersions{allowed: []string{"0.100.0", "0.162.1"}, target: "0.100.0"}
}

func TestCodexVersionLifecycle(t *testing.T) {
	for _, mode := range []string{"updated", "current", "registry_unavailable", "no_allowed_release", "download_failed", "stage_failed", "publish_failed", "probe_failed", "reset_failed", "restore_failed", "forbidden", "empty", "invalid_install"} {
		t.Run(mode, func(t *testing.T) {
			v := &fakeVersions{allowed: []string{"0.162.1", "0.162.10"}, target: "0.162.10", latest: "0.163.0"}
			w := &world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.10", "codex-cli 0.162.10"}}
			want := mode
			switch mode {
			case "current":
				v.target = "0.162.1"
			case "registry_unavailable":
				v.registryErr = errors.New("offline")
				w.versions = []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.1"}
			case "no_allowed_release":
				v.target = ""
				w.versions = []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.1"}
			case "download_failed":
				v.binaryErr = errors.New("bad integrity")
				w.versions = []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.1"}
				want = "update_failed"
			case "stage_failed", "publish_failed", "probe_failed", "reset_failed", "restore_failed":
				want = "update_failed"
				w.versions = []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.1", "codex-cli 0.162.1"}
				w.execErr = func(args []string) error {
					if slices.Contains(args, strings.TrimSuffix(mode, "_failed")) || (mode == "reset_failed" && slices.Contains(args, "stage")) || (mode == "restore_failed" && slices.Contains(args, "stage")) {
						return errors.New("failed")
					}
					if mode == "restore_failed" && slices.Contains(args, "--version") && slices.Contains(args, "/opt/agent-pm/bin/codex") {
						return errors.New("restore failed")
					}
					return nil
				}
			case "forbidden":
				v.allowed = []string{"0.162.10"}
			case "empty":
				v.allowed = []string{}
			case "invalid_install":
				v.verifyErr = errors.New("untrusted")
			}
			h, d := startHost(w, &fakeCreds{})
			h.Versions = v
			var progress bytes.Buffer
			h.Progress = &progress
			got, err := h.Start(context.Background(), validManifest(t))
			blocked := slices.Contains([]string{"reset_failed", "restore_failed", "forbidden", "empty", "invalid_install"}, mode)
			if (err != nil) != blocked {
				t.Fatalf("%+v %v", got, err)
			}
			injected := false
			for _, call := range d.calls {
				if slices.Contains(call, "inject") {
					injected = true
				}
			}
			if injected == blocked {
				t.Fatalf("secret ordering: %v %v", injected, err)
			}
			if blocked {
				return
			}
			if got.ClientUpdate.Result != want || got.ClientUpdate.PinnedVersion != "0.162.1" || got.ClientUpdate.ImageVersion != "0.162.1" {
				t.Fatalf("%+v", got.ClientUpdate)
			}
			expected := "codex-cli 0.162.1"
			if mode == "updated" {
				expected = "codex-cli 0.162.10"
				if v.arch != "arm64" {
					t.Fatal(v.arch)
				}
			}
			if got.Clients.Codex != expected {
				t.Fatalf("actual %s", got.Clients.Codex)
			}
			if !strings.Contains(progress.String(), "Codex update "+want) {
				t.Fatal(progress.String())
			}
			if mode != "registry_unavailable" && !strings.Contains(progress.String(), "sudo /usr/local/libexec/agent-pm/agent-pm-broker update-codex 0.163.0") {
				t.Fatal("missing upgrade hint")
			}
			var run []string
			for _, call := range d.calls {
				if len(call) > 1 && call[0] == "run" && call[1] == "--detach" {
					run = call
				}
			}
			if !strings.Contains(strings.Join(run, " "), "/opt/agent-pm/runtime:rw,exec,nosuid,nodev,") {
				t.Fatal("missing volatile executable mount")
			}
		})
	}
}

func TestCodexUpdateBeforeAllSecrets(t *testing.T) {
	for _, client := range []string{"claude", "codex"} {
		for _, blocked := range []bool{false, true} {
			t.Run(client+map[bool]string{true: "-rejected"}[blocked], func(t *testing.T) {
				v := &fakeVersions{allowed: []string{"0.162.1", "0.162.10"}, target: "0.162.10"}
				if blocked {
					v.allowed = []string{"0.162.10"}
				}
				h, d := startHost(&world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.162.1", "codex-cli 0.162.10", "codex-cli 0.162.10"}}, &fakeCreds{})
				h.Versions = v
				h.Codex = &fakeCodexCreds{}
				manifest := githubManifest(t)
				if client == "codex" {
					b, _ := os.ReadFile(codexManifest(t))
					var m map[string]any
					if err := json.Unmarshal(b, &m); err != nil {
						t.Fatal(err)
					}
					m["github"] = map[string]string{"app": "agent-pm-bot"}
					b, _ = json.Marshal(m)
					manifest = writeManifest(t, b)
				}
				_, err := h.Start(context.Background(), manifest)
				if (err != nil) != blocked {
					t.Fatal(err)
				}
				stage, publish, probe, secrets := -1, -1, -1, 0
				for i, call := range d.calls {
					if slices.Contains(call, "stage") {
						stage = i
					}
					if slices.Contains(call, "publish") {
						publish = i
					}
					if slices.Contains(call, "--version") {
						probe = i
					}
					if slices.Contains(call, "inject") || slices.Contains(call, "inject-codex") || slices.Contains(call, "write") {
						secrets++
						if blocked || !(stage < publish && publish < probe && probe < i) {
							t.Fatalf("secret before verified update: %v", d.calls)
						}
					}
				}
				if (!blocked && secrets != 2) || (blocked && secrets != 0) {
					t.Fatal(secrets)
				}
			})
		}
	}
}
