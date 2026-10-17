// Package docker is the only code that runs the Docker CLI: fixed binary, fixed endpoint, clean environment.
package docker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const (
	Path       = "/Applications/Docker.app/Contents/Resources/bin/docker"
	pluginDir  = "/Applications/Docker.app/Contents/Resources/cli-plugins"
	SecretsDir = "/run/agent-pm/secrets"
	capture    = 65536
)

type Runner interface {
	Run(ctx context.Context, args []string, stdin []byte) ([]byte, error)
}

var ErrUnreachable = errors.New("Docker Desktop daemon unreachable")

type CommandError struct {
	Exit   int
	Stderr string
}

func (e *CommandError) Error() string {
	return fmt.Sprintf("docker exited %d: %s", e.Exit, strings.TrimSpace(e.Stderr))
}

// Diagnostic is Docker's diagnostic: the last stderr line that is not the CLI's --help trailer.
func Diagnostic(err error) string {
	var ce *CommandError
	if !errors.As(err, &ce) {
		return err.Error()
	}
	lines := strings.Split(ce.Stderr, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line != "" && !strings.HasPrefix(line, "Run 'docker ") && !strings.HasPrefix(line, "See 'docker ") {
			return line
		}
	}
	return err.Error()
}

// Absent reports whether Docker says the image, object or container does not exist.
func Absent(err error) bool {
	var ce *CommandError
	return errors.As(err, &ce) && (strings.Contains(ce.Stderr, "No such image") || strings.Contains(ce.Stderr, "No such object") || strings.Contains(ce.Stderr, "No such container"))
}

type CLI struct {
	path, home, config string
}

// New checks Docker Desktop's executables and writes a private CLI config that ignores ~/.docker.
func New(home string) (*CLI, func(), error) {
	for _, path := range []string{Path, filepath.Join(pluginDir, "docker-buildx")} {
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode()&0o111 == 0 {
			return nil, nil, fmt.Errorf("Docker Desktop executable unavailable: %s", path)
		}
	}
	dir, err := os.MkdirTemp("", "agent-pm-docker-")
	if err != nil {
		return nil, nil, err
	}
	clean := func() { _ = os.RemoveAll(dir) }
	config, _ := json.Marshal(map[string]any{"cliPluginsExtraDirs": []string{pluginDir}})
	if err := os.WriteFile(filepath.Join(dir, "config.json"), config, 0o600); err != nil {
		clean()
		return nil, nil, err
	}
	return &CLI{path: Path, home: home, config: dir}, clean, nil
}

func (c *CLI) Endpoint() string {
	return "unix://" + filepath.Join(c.home, ".docker/run/docker.sock")
}

func (c *CLI) Command(args []string) (string, []string, []string) {
	argv := append([]string{"--config", c.config, "--host", c.Endpoint()}, args...)
	env := []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=" + c.home, "DOCKER_CONFIG=" + c.config, "DOCKER_BUILDKIT=1"}
	return c.path, argv, env
}

type capped struct{ data []byte }

func (b *capped) Write(p []byte) (int, error) {
	if left := capture - len(b.data); left > 0 {
		b.data = append(b.data, p[:min(len(p), left)]...)
	}
	return len(p), nil
}

func (c *CLI) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	path, argv, env := c.Command(args)
	cmd := exec.CommandContext(ctx, path, argv...)
	cmd.Env = env
	cmd.Stdin = bytes.NewReader(stdin)
	var stdout, stderr capped
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if err == nil {
		return stdout.data, nil
	}
	text := string(stderr.data)
	if strings.Contains(text, "Cannot connect to the Docker daemon") || strings.Contains(text, "docker.sock: connect:") {
		return nil, fmt.Errorf("%w: %s", ErrUnreachable, strings.TrimSpace(text))
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && ctx.Err() == nil {
		return nil, &CommandError{Exit: exit.ExitCode(), Stderr: text}
	}
	return nil, fmt.Errorf("docker: %w", errors.Join(err, ctx.Err()))
}
