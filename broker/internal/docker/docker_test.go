package docker

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestBoundary(t *testing.T) {
	t.Setenv("HOME", "/hostile")
	t.Setenv("DOCKER_HOST", "tcp://hostile:1234")
	t.Setenv("DOCKER_CONTEXT", "hostile")
	t.Setenv("PATH", "/hostile")
	c := &CLI{path: Path, home: "/Users/test", config: "/private/config"}
	path, args, env := c.Command([]string{"inspect", "image"})
	want := []string{"--config", "/private/config", "--host", "unix:///Users/test/.docker/run/docker.sock", "inspect", "image"}
	if path != "/Applications/Docker.app/Contents/Resources/bin/docker" || !reflect.DeepEqual(args, want) {
		t.Fatalf("unexpected command %s %q", path, args)
	}
	wantEnv := []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=/Users/test", "DOCKER_CONFIG=/private/config", "DOCKER_BUILDKIT=1"}
	if !reflect.DeepEqual(env, wantEnv) {
		t.Fatalf("env %q", env)
	}
	if c.Endpoint() != "unix:///Users/test/.docker/run/docker.sock" {
		t.Fatal(c.Endpoint())
	}
}

func fakeDocker(t *testing.T, script string) *CLI {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "docker")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	return &CLI{path: path, buildx: path, home: dir, config: dir}
}

func TestRunResults(t *testing.T) {
	out, err := fakeDocker(t, `cat; echo " $*"`).Run(context.Background(), []string{"ps"}, []byte("in"))
	if err != nil || !strings.HasPrefix(string(out), "in --config ") || !strings.HasSuffix(string(out), " ps\n") {
		t.Fatalf("%q %v", out, err)
	}
	_, err = fakeDocker(t, "echo nope >&2; exit 3").Run(context.Background(), nil, nil)
	var ce *CommandError
	if !errors.As(err, &ce) || ce.Exit != 3 || ce.Stderr != "nope\n" || errors.Is(err, ErrUnreachable) {
		t.Fatalf("%#v", err)
	}
}

func TestUnreachable(t *testing.T) {
	for _, msg := range []string{
		"Cannot connect to the Docker daemon at unix:///Users/x/.docker/run/docker.sock. Is the docker daemon running?",
		"error during connect: dial unix /Users/x/.docker/run/docker.sock: connect: no such file or directory",
	} {
		_, err := fakeDocker(t, "echo '"+msg+"' >&2; exit 1").Run(context.Background(), nil, nil)
		if !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%q: %v", msg, err)
		}
	}
}

func TestNewNeedsNoDockerDesktop(t *testing.T) {
	_, clean, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	clean()
}

func TestMissingExecutableIsUnreachable(t *testing.T) {
	present := fakeDocker(t, "touch ran").path
	for _, c := range []*CLI{{path: "/nonexistent/docker", buildx: present}, {path: present, buildx: "/nonexistent/docker-buildx"}} {
		_, err := c.Run(context.Background(), []string{"ps"}, nil)
		if !errors.Is(err, ErrUnreachable) || !strings.Contains(err.Error(), "executable unavailable: /nonexistent/") {
			t.Fatalf("%q: %v", c.path, err)
		}
	}
}
