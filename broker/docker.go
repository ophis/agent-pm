package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const dockerPath = "/Applications/Docker.app/Contents/Resources/bin/docker"
const pluginDir = "/Applications/Docker.app/Contents/Resources/cli-plugins"

type runner interface {
	Run(context.Context, []string, []byte) ([]byte, error)
}

type dockerCLI struct {
	home, config string
}

func newDocker(home string) (*dockerCLI, func(), error) {
	for _, path := range []string{dockerPath, filepath.Join(pluginDir, "docker-buildx")} {
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode()&0111 == 0 {
			return nil, nil, fmt.Errorf("Docker Desktop executable unavailable: %s", path)
		}
	}
	dir, err := os.MkdirTemp("", "agent-pm-docker-")
	if err != nil {
		return nil, nil, err
	}
	clean := func() { _ = os.RemoveAll(dir) }
	config, _ := json.Marshal(map[string]any{"cliPluginsExtraDirs": []string{pluginDir}})
	if err := os.WriteFile(filepath.Join(dir, "config.json"), config, 0600); err != nil {
		clean()
		return nil, nil, err
	}
	return &dockerCLI{home: home, config: dir}, clean, nil
}

func (d dockerCLI) endpoint() string {
	return "unix://" + filepath.Join(d.home, ".docker/run/docker.sock")
}

func (d dockerCLI) command(args []string) (string, []string, []string) {
	argv := append([]string{"--config", d.config, "--host", d.endpoint()}, args...)
	env := []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=" + d.home, "DOCKER_CONFIG=" + d.config, "DOCKER_BUILDKIT=1"}
	return dockerPath, argv, env
}

type cappedBuffer struct{ data []byte }

func (b *cappedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if left := 65536 - len(b.data); left > 0 {
		if len(p) > left {
			p = p[:left]
		}
		b.data = append(b.data, p...)
	}
	return n, nil
}

func (d dockerCLI) Run(ctx context.Context, args []string, input []byte) ([]byte, error) {
	path, argv, env := d.command(args)
	cmd := exec.CommandContext(ctx, path, argv...)
	cmd.Env = env
	cmd.Stdin = strings.NewReader(string(input))
	var stdout, stderr cappedBuffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("Docker: %w: %s", err, stderr.data)
	}
	return stdout.data, nil
}

var _ io.Writer = (*cappedBuffer)(nil)
