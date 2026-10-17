package launcher

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const (
	python    = "/opt/agent-pm/venv/bin/python"
	bootstrap = "/opt/agent-pm/lib/bootstrap.py"
	sharing   = "Docker Desktop file sharing must include ~/.agent-pm/docker-state (Settings > Resources > File sharing)."
	cache     = ".agent-pm-cache"
)

// errTaken: another start created the container first, so it is not this start's to roll back.
var errTaken = errors.New("container name taken")

func asAgent(container string, argv ...string) []string {
	return append([]string{"exec", "--user", "agent", container}, argv...)
}

// probe reads both clients' versions; the image installed them.
func (h Host) probe(ctx context.Context, container string) (Clients, error) {
	var versions [2]string
	var errs []error
	for i, client := range []string{"claude", "codex"} {
		out, err := h.Docker.Run(ctx, asAgent(container, client, "--version"), nil)
		line, _, _ := strings.Cut(strings.TrimSpace(string(out)), "\n")
		versions[i] = strings.TrimSpace(line)
		if err == nil && (versions[i] == "" || hasControl(versions[i])) {
			err = errors.New(client + " --version gave no usable version")
		}
		errs = append(errs, err)
	}
	return Clients{Claude: versions[0], Codex: versions[1]}, errors.Join(errs...)
}

func taken(c string) *protocol.Error {
	return protocol.New(protocol.StartFailed, "Container "+c+" already exists; close it first.")
}

// Start writes each phase's seconds to Progress only once it succeeds, so a failure's stderr stays one error object.
func (h Host) Start(ctx context.Context, path string) (StartResult, error) {
	var times strings.Builder
	began := time.Now()
	last := began
	lap := func(phase string) {
		now := time.Now()
		fmt.Fprintf(&times, "agent-pm-broker: start %s %.2f s\n", phase, now.Sub(last).Seconds())
		last = now
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return StartResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return StartResult{}, err
	}
	if err := h.checkMounts(m); err != nil {
		return StartResult{}, err
	}
	if err := h.Creds.Ready(); err != nil {
		return StartResult{}, err
	}
	lap("preflight")
	info, err := h.checkImage(ctx, m.Image)
	if err != nil {
		return StartResult{}, err
	}
	lap("image")
	c := "agent-pm-" + m.Name + "-" + m.Generation
	if _, err := h.Docker.Run(ctx, []string{"container", "inspect", c}, nil); !docker.Absent(err) {
		if err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Cannot check for container "+c+".")
		}
		return StartResult{}, taken(c)
	}
	if err := h.ensureVolume(ctx, m, info); err != nil {
		return StartResult{}, err
	}
	lap("checks")
	result, err := h.create(ctx, c, m, info, lap)
	if errors.Is(err, errTaken) {
		return StartResult{}, taken(c)
	}
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.StartFailed, "Start failed.")
		}
		h.rollback(ctx, c, e)
		return StartResult{}, e
	}
	fmt.Fprintf(&times, "agent-pm-broker: start total %.2f s\n", time.Since(began).Seconds())
	if h.Progress != nil {
		_, _ = io.WriteString(h.Progress, times.String())
	}
	return result, nil
}

// create runs every step after the absence check; any error makes Start roll back.
func (h Host) create(ctx context.Context, c string, m Manifest, info imageInfo, lap func(phase string)) (StartResult, error) {
	labels := info.Config.Labels
	args := []string{"run", "--detach", "--name", c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent",
		"--network", "bridge", "--workdir", m.CWD, "--env", "CLAUDE_CONFIG_DIR=" + m.ClaudeDir}
	for _, key := range []string{"npm_config_cache", "UV_CACHE_DIR", "PIP_CACHE_DIR"} {
		args = append(args, "--env", key+"="+filepath.Join(m.CWD, cache))
	}
	args = append(args,
		"--label", "agent-pm.session="+m.Name, "--label", "agent-pm.generation="+m.Generation)
	keys := make([]string, 0, len(m.Labels))
	for k := range m.Labels {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	for _, k := range keys {
		args = append(args, "--label", k+"="+m.Labels[k])
	}
	args = append(args, "--mount", volumeMount(m.Workspace, m.CWD, false), "--mount", bindMount(m.ClaudeDir, m.ClaudeDir),
		"--tmpfs", docker.SecretsDir+":rw,noexec,nosuid,nodev,size=65536,mode=0700,uid="+labels["agent-pm.agent-uid"]+",gid="+labels["agent-pm.agent-gid"],
		"--entrypoint", "/usr/bin/tini", info.ID, "--", "sleep", "infinity")
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		if docker.Conflict(err) {
			return StartResult{}, errTaken
		}
		e := diagnosed(err, protocol.StartFailed, "Creating the container failed.")
		if text := err.Error(); strings.Contains(text, "mounts denied") || strings.Contains(text, "not shared from the host") {
			e.Message = sharing
		}
		return StartResult{}, e
	}
	lap("create")
	if _, err := h.Docker.Run(ctx, asAgent(c, python, "-I", bootstrap, "claude", m.ClaudeDir, m.CWD), nil); err != nil {
		return StartResult{}, failure(err, protocol.StartFailed, "Writing Claude's bootstrap state failed.")
	}
	if m.HasHook {
		if _, err := h.Docker.Run(ctx, []string{"exec", "--user", "root", c, python, "-I", bootstrap, "hook", m.HookPath}, nil); err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Installing the hook stub failed.")
		}
	}
	lap("bootstrap")
	clients, err := h.probe(ctx, c)
	if err != nil {
		return StartResult{}, failure(err, protocol.StartFailed, "Cannot run the image's clients.")
	}
	lap("clients")
	exec := func(argv []string, stdin []byte) error {
		_, err := h.Docker.Run(ctx, append([]string{"exec", "--interactive", "--user", "agent", c}, argv...), stdin)
		return err
	}
	if err := h.Creds.Inject(exec); err != nil {
		var e *protocol.Error
		if errors.As(err, &e) {
			return StartResult{}, e
		}
		if errors.Is(err, docker.ErrUnreachable) {
			return StartResult{}, failure(err, protocol.StartFailed, "")
		}
		return StartResult{}, protocol.New(protocol.StartFailed, "Injecting the Claude token failed.")
	}
	lap("credentials")
	return StartResult{Container: c, Endpoint: h.Endpoint, ImageDigest: info.ID, Clients: clients}, nil
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
