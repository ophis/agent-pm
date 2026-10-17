package launcher

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const (
	python    = "/opt/agent-pm/venv/bin/python"
	bootstrap = "/opt/agent-pm/lib/bootstrap.py"
	sharing   = "Docker Desktop file sharing must include cwd and ~/.agent-pm/docker-state (Settings > Resources > File sharing)."
)

// bindMount renders a --mount value; Docker parses it as one CSV record.
func bindMount(source, target string) string {
	var b strings.Builder
	w := csv.NewWriter(&b)
	_ = w.Write([]string{"type=bind", "source=" + source, "target=" + target})
	w.Flush()
	return strings.TrimSuffix(b.String(), "\n")
}

func numeric(s string) bool {
	n, err := strconv.ParseUint(s, 10, 31)
	return err == nil && n > 0
}

// checkImage requires a broker-built image: matching key, version and agent ids in its labels.
func (h Host) checkImage(ctx context.Context, image string) (imageInfo, error) {
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return imageInfo{}, failure(err, protocol.ImageRejected, "Cannot inspect image "+image+".")
	}
	l := info.Config.Labels
	if !found || l["agent-pm.key"] != strings.TrimPrefix(image, "agent-pm-worker:") || l["agent-pm.version"] == "" ||
		!numeric(l["agent-pm.agent-uid"]) || !numeric(l["agent-pm.agent-gid"]) {
		return imageInfo{}, protocol.New(protocol.ImageRejected, "Image "+image+" is not a broker-built worker image; run agent-pm-broker build.")
	}
	return info, nil
}

// checkNesting refuses a cwd strictly inside a running worker's bind source: that worker could swap
// a directory for a symlink between this check and Docker's mount.
func (h Host) checkNesting(ctx context.Context, cwd string) error {
	out, err := h.Docker.Run(ctx, []string{"ps", "--filter", "label=agent-pm.session", "--format", "{{.ID}}"}, nil)
	if err != nil {
		return failure(err, protocol.StartFailed, "Cannot list running workers.")
	}
	ids := strings.Fields(string(out))
	if len(ids) == 0 {
		return nil
	}
	out, err = h.Docker.Run(ctx, append([]string{"inspect", "--format", "{{json .Mounts}}"}, ids...), nil)
	if err != nil {
		return failure(err, protocol.StartFailed, "Cannot inspect running workers.")
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		var mounts []struct{ Type, Source string }
		if err := json.Unmarshal([]byte(line), &mounts); err != nil {
			return protocol.New(protocol.StartFailed, "Cannot read running workers' mounts.")
		}
		for _, m := range mounts {
			if m.Type == "bind" && m.Source != "" && h.within(cwd, m.Source) && !h.Same(cwd, m.Source) {
				return protocol.Newf(protocol.MountRejected, "cwd %s is inside running worker mount %s.", cwd, m.Source)
			}
		}
	}
	return nil
}

func (h Host) Start(ctx context.Context, path string) (StartResult, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return StartResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return StartResult{}, err
	}
	cwd, err := h.checkMounts(m)
	if err != nil {
		return StartResult{}, err
	}
	if err := h.Creds.Ready(); err != nil {
		return StartResult{}, err
	}
	info, err := h.checkImage(ctx, m.Image)
	if err != nil {
		return StartResult{}, err
	}
	if err := h.checkNesting(ctx, cwd); err != nil {
		return StartResult{}, err
	}
	c := "agent-pm-" + m.Name + "-" + m.Generation
	if _, err := h.Docker.Run(ctx, []string{"container", "inspect", c}, nil); !docker.Absent(err) {
		if err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Cannot check for container "+c+".")
		}
		return StartResult{}, protocol.New(protocol.StartFailed, "Container "+c+" already exists; close it first.")
	}
	result, err := h.create(ctx, c, m, cwd, info)
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.StartFailed, "Start failed.")
		}
		h.rollback(ctx, c, e)
		return StartResult{}, e
	}
	return result, nil
}

// create runs every step after the absence check; any error makes Start roll back.
func (h Host) create(ctx context.Context, c string, m Manifest, cwd string, info imageInfo) (StartResult, error) {
	labels := info.Config.Labels
	args := []string{"run", "--detach", "--name", c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent",
		"--network", "bridge", "--workdir", m.CWD, "--env", "CLAUDE_CONFIG_DIR=" + m.ClaudeDir,
		"--label", "agent-pm.session=" + m.Name, "--label", "agent-pm.generation=" + m.Generation}
	keys := make([]string, 0, len(m.Labels))
	for k := range m.Labels {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	for _, k := range keys {
		args = append(args, "--label", k+"="+m.Labels[k])
	}
	args = append(args, "--mount", bindMount(cwd, m.CWD), "--mount", bindMount(m.ClaudeDir, m.ClaudeDir),
		"--tmpfs", docker.SecretsDir+":rw,noexec,nosuid,nodev,size=65536,mode=0700,uid="+labels["agent-pm.agent-uid"]+",gid="+labels["agent-pm.agent-gid"],
		"--entrypoint", "/usr/bin/tini", info.ID, "--", "sleep", "infinity")
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		e := diagnosed(err, protocol.StartFailed, "Creating the container failed.")
		if text := err.Error(); strings.Contains(text, "mounts denied") || strings.Contains(text, "not shared from the host") {
			e.Message = sharing
		}
		return StartResult{}, e
	}
	if _, err := h.Docker.Run(ctx, asAgent(c, python, "-I", bootstrap, "claude", m.ClaudeDir, m.CWD), nil); err != nil {
		return StartResult{}, failure(err, protocol.StartFailed, "Writing Claude's bootstrap state failed.")
	}
	if m.HasHook {
		if _, err := h.Docker.Run(ctx, []string{"exec", "--user", "root", c, python, "-I", bootstrap, "hook", m.HookPath}, nil); err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Installing the hook stub failed.")
		}
	}
	clients, update, err := h.updateClients(ctx, c)
	if err != nil {
		return StartResult{}, err
	}
	if err := h.Creds.Inject(ctx, c, h.Docker); err != nil {
		if errors.Is(err, docker.ErrUnreachable) {
			return StartResult{}, failure(err, protocol.StartFailed, "")
		}
		return StartResult{}, protocol.New(protocol.StartFailed, "Injecting the Claude token failed.")
	}
	return StartResult{Container: c, Endpoint: h.Endpoint, ImageDigest: info.ID, Clients: clients, Update: update}, nil
}

// rollback deletes c on a fresh context so a signal or cancelled caller still cleans up.
func (h Host) rollback(ctx context.Context, c string, e *protocol.Error) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	removed, err := h.remove(rctx, c)
	switch {
	case err != nil:
		e.Cleanup, e.CleanupError, e.Container = protocol.Failed, docker.Diagnostic(err), c
	case removed:
		e.Cleanup = protocol.Removed
	default:
		e.Cleanup = protocol.NotNeeded
	}
}
