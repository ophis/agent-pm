package launcher

import (
	"context"
	"slices"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

func TestUpdateOutcomes(t *testing.T) {
	npm := func(err error) func([]string) error { return failOn("npm", err) }
	for name, tc := range map[string]struct {
		w      *world
		reason string
		fatal  bool
	}{
		"network":   {&world{execErr: npm(&docker.CommandError{Exit: 1, Stderr: "npm error code ENOTFOUND\nnpm error network request failed"})}, "network", false},
		"eai_again": {&world{execErr: npm(&docker.CommandError{Exit: 1, Stderr: "npm error code EAI_AGAIN"})}, "network", false},
		"timeout":   {&world{execErr: npm(&docker.CommandError{Exit: 124, Stderr: ""})}, "timeout", false},
		"killed":    {&world{execErr: npm(&docker.CommandError{Exit: 137, Stderr: ""})}, "timeout", false},
		"command":   {&world{execErr: npm(&docker.CommandError{Exit: 1, Stderr: "npm error code E403"})}, "command_failed", false},
		"version":   {&world{versions: []string{"", "codex-cli 1\n", "2.0.0\n", "codex-cli 0.9\n"}}, "version_failed", false},
		"restore fails": {&world{execErr: func(a []string) error {
			if slices.Contains(a, "npm") || slices.Contains(a, "/bin/sh") {
				return &docker.CommandError{Exit: 1, Stderr: "x"}
			}
			return nil
		}}, "", true},
		"post-restore probe fails": {&world{execErr: npm(&docker.CommandError{Exit: 1}), versions: []string{"", ""}}, "", true},
		"control in version":       {&world{versions: []string{"\x1b[31m2.0\n", "codex\n", "2.0\n", "codex\n"}}, "version_failed", false},
	} {
		t.Run(name, func(t *testing.T) {
			tc.w.labels = goodLabels()
			h, d := startHost(tc.w, &fakeCreds{})
			got, err := h.Start(context.Background(), validManifest(t))
			if tc.fatal {
				if e := protoErr(t, err); e.Code != protocol.StartFailed || e.Cleanup != protocol.Removed {
					t.Fatalf("%+v", e)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got.Update.Status != "fallback" || got.Update.Reason != tc.reason || got.Update.Message == "" || got.Clients.Claude == "" || got.Clients.Codex == "" {
				t.Fatalf("%+v", got)
			}
			restored := slices.ContainsFunc(d.calls, func(c []string) bool {
				return slices.Equal(c[len(c)-3:], []string{"/bin/sh", "-c", "rm -rf /home/agent/.npm-global && cp -a /opt/agent-pm/client-baseline /home/agent/.npm-global"})
			})
			if !restored {
				t.Fatalf("no restore: %q", d.calls)
			}
		})
	}
}
